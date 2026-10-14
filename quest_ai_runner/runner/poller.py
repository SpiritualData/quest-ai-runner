"""Poller — the watchdog generalized into Quest's missing executor lane.

This is the personal ``personal_watchdog.py`` made generic. The reusable essence it preserves:
  * EVENT-DRIVEN discovery (fire when a task is DUE, not on a blind clock) — poll mode is the
    floor; the same loop accepts webhook/subscribe transports later (one executor behind all).
  * SIGNATURE DEDUP across restarts — a JSON state store records handled task signatures so a
    task is claimed/run exactly once even if the process restarts (watchdog_state.json pattern).
  * BACKEND-AWARE CLAIM — claiming a task PATCHes it to in_progress, so the backend stops it
    re-firing; the local signature store is a belt-and-suspenders second guard.
  * GRACEFUL DEGRADATION — unconfigured key -> log + exit 0; a transient API error in one scan
    is logged and the loop continues; a bad spawn never kills the poller.
  * BOUNDED CONCURRENCY — at most ``max_concurrent_tasks`` run at once (the async-spawn essence,
    bounded). The poller claims, hands each task to the TaskExecutor, and reports back.
  * RESOURCE-AWARE PICKUP (opt-in) — when the host is overloaded (memory/load limits from
    ``ResourceLimits``), the poller pauses NEW task pickup instead of thrashing: an unclaimed
    task stays queued on the backend, so it simply runs on a later scan once resources recover.

Run modes (like the watchdog): ``run_once()`` for cron, ``run_forever()`` for a service.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..config import RunnerConfig, build_orchestrator, derive_capabilities, resolve_rep_sync_resolver
from ..core import usage_limit
from ..resources import ResourceGuard, ResourceLimits
from .goal_handoff import make_judge
from .autopilot import AutopilotPass, _parse_dt
from .context_updates import build_update_engine


















from .executor import TaskExecutor
from .quest_client import QuestApiError, QuestClient, QuestDecisionSink, QuestNotConfigured
# StateStore lives in its own module (runner/state_store.py) so the channel-runner lane can reuse
# the SAME dedup mechanism without duplicating it. Re-exported here (not just imported for local
# use) so `from quest_ai_runner.runner.poller import StateStore` keeps working unchanged.
from .state_store import StateStore

from .poller_schedule import (
    CLAIM_RETRY_COOLDOWN_SECONDS,
    _due_now_locally,
    _foreign_series_looks_alive,
    _task_signature,
    log,
)
from .poller_passes import PassesMixin
from .poller_sync import SyncMixin


def _guidance_manager_of(cfg: Any) -> Any:
    """The card store a standing instruction should be written to, or None.

    Duck-typed across the shapes a deployment may have wired: an explicit manager on the config, or
    the one its guidance provider is already reading. A deployment with neither keeps tracking
    standing rules in the ledger; they simply do not become retrievable cards.
    """
    manager = getattr(cfg, "guidance_manager", None)
    if manager is not None:
        return manager
    provider = getattr(cfg, "guidance_provider", None)
    for attr in ("manager", "cards", "card_manager"):
        found = getattr(provider, attr, None) if provider is not None else None
        if found is not None:
            return found
    return None


__all__ = ["Poller", "StateStore"]




class Poller(PassesMixin, SyncMixin):
    def __init__(self, config: RunnerConfig, *, state_path: Optional[str] = None,
                 client: Optional[QuestClient] = None,
                 resource_guard: Optional[ResourceGuard] = None,
                 now: Optional[Callable[[], datetime]] = None):
        self.cfg = config
        # Injectable clock, used by the per-quest pass scheduling (retune/catch-up, see
        # ``_expected_quest_occurrence``) so tests can assert against a frozen instant instead of
        # the wall clock. Defaults to the real, timezone-AWARE current moment (UTC) -- every
        # timezone conversion downstream needs an aware instant to convert correctly regardless of
        # the host's own zone.
        self._now = now or (lambda: datetime.now(timezone.utc))
        # TTL-cached quest autopilot-schedule snapshot (see ``_quest_schedule_snapshot``). None
        # means "never read yet"; refreshed at most every ``cfg.autopilot_settings_refresh_seconds``.
        self._quest_schedule_cache: Dict[str, Dict[str, Any]] = {}
        self._quest_schedule_cache_at: Optional[float] = None
        # Resource-aware pickup (opt-in): explicit guard > config limits > QAR_* env vars.
        # With nothing configured the guard is disabled and every check is a cheap no-op.
        if resource_guard is None:
            limits = config.resource_limits if config.resource_limits is not None \
                else ResourceLimits.from_env()
            resource_guard = ResourceGuard(limits)
        self.resources = resource_guard
        # Daily token budget: consumer-supplied tracker > auto-created from env.
        # ON BY DEFAULT (2M tokens/day) so an unconfigured deployment cannot rack up unexpected
        # API charges. Override with QAR_DAILY_TOKEN_LIMIT=<n>; disable with QAR_DAILY_TOKEN_LIMIT=0.
        if config.usage_tracker is None:
            try:
                from ..usage import DailyUsageTracker
                config.usage_tracker = DailyUsageTracker.from_env()
            except Exception:  # noqa: BLE001 — tracking must never break the runner
                log.debug("daily usage tracker could not be created; token limit disabled",
                          exc_info=True)
        self._usage_tracker = config.usage_tracker
        self.client = client or QuestClient(
            config.quest_base_url, config.quest_api_key, team_id=config.team_id)
        # Default the escalation sink to a Quest decision-request sink if the consumer didn't set one.
        if config.escalation is None and self.client.configured:
            config.escalation = QuestDecisionSink(
                self.client, default_assignee_user_id=config.default_assignee_user_id,
                default_deadline_hours=config.decision_default_deadline_hours)
        # Personas (opt-in; see runner/personas.py + RunnerConfig.personas): compose a
        # rep_sync_resolver from declarative config when the consumer did not already wire one by
        # hand. Same in-place write-back style build_orchestrator uses for deep_runner — a consumer
        # or test that reads config.rep_sync_resolver after construction sees the resolved callable.
        config.rep_sync_resolver = resolve_rep_sync_resolver(config, quest_client=self.client)
        self.state = StateStore(state_path)
        # The lane's Claude usage-limit note lives beside its state file, so a lane restarted
        # mid-pause keeps honouring the reset instead of re-discovering the limit task by task.
        if state_path:
            usage_limit.configure_persistence(
                str(Path(state_path).with_name(Path(state_path).stem + "_usage_limit.json")))
        # One-shot: abandoned claims are reconciled on this process's first scan (_recover_orphans).
        self._orphans_reconciled = False
        self._orchestrator = None  # built lazily so an unconfigured poll degrades cleanly
        # Autopilot: built once (stateless other than the injected client/config) and handed to
        # every TaskExecutor this poller builds, so a task with ``handler == "autopilot"`` routes
        # to it instead of a deep run. Inert unless such a task is ever discovered.
        # Automated context updates (runner/context_updates.py): ONE engine per poller, shared by
        # every pass it runs, because the engine caches the user-scoped reads (reflections,
        # captures) for its lifetime -- a fresh engine per quest would re-read them per quest.
        self._update_engine = build_update_engine(config, self.client, state_path=state_path)
        self._autopilot = AutopilotPass(
            self.client,
            team_id=config.team_id or "",
            # Which team each quest's autopilot work belongs on, for a lane serving several. The
            # pass object is shared across the poller's thread pool, so this is a pure function of
            # quest_id rather than state the pass carries per run.
            team_resolver=self.autopilot_team_for,
            team_ids=list(config.team_ids),
            persona_resolver=config.autopilot_persona_resolver,
            daily_budget=config.autopilot_daily_budget,
            backpressure=config.autopilot_backpressure,
            adopt_recurring_default=config.autopilot_adopt_recurring,
            # Same map the folder sync uses, so a quest whose folder is already synced also gets
            # its canonical next-steps artifact read and refreshed by each pass.
            quest_folder_map=config.quest_folder_map,
            # What has changed on each quest since a run last looked at it, on every channel the
            # quest itself asks for. None when the consumer switched it off.
            update_engine=self._update_engine,
            # The lane's own account, stamped as the assignee of the work a pass creates, so the
            # lane that created it can discover and run it (discovery is owner-scoped). Without
            # it the backend makes the quest's human owner the executor and the batch is
            # invisible to every lane -- see AutopilotPass._lane_user_id.
            lane_user_id=config.lane_user_id,
            judge=make_judge(getattr(config, "model_provider", None)),
        )
        # Capabilities this runner can HONESTLY report, derived from the wired adapters
        # (corpus=FilesAdapter/corpus, code=deep-runner, web=deep-runner can browse via Claude
        # Code's WebSearch/WebFetch). Computed once at construction (the adapter wiring is fixed
        # for the poller's lifetime).
        self._capabilities = derive_capabilities(config)
        # In-process claim guard: prevents the background scan and the fast lane (wait channel /
        # fallback poll, see run_forever) from BOTH claiming and running the SAME task when they
        # observe it in the same short window before either has PATCHed it off 'queued'. This is
        # belt-and-suspenders alongside the backend's own claim() PATCH -- it only protects against
        # a race WITHIN this one process.
        self._inflight_lock = threading.Lock()
        self._inflight: set = set()
        # task_id -> monotonic time before which dispatch skips it: a task whose claim failed
        # (another account owns it, so the PATCH 404s) must not keep taking a worker slot.
        self._claim_cooldown: Dict[str, float] = {}

    def _orch(self):
        if self._orchestrator is None:
            self._orchestrator = build_orchestrator(self.cfg)
        return self._orchestrator

    # --- one scan ------------------------------------------------------------

    def run_once(self) -> List[str]:
        """One housekeeping + dispatch pass (``poll --once``). Returns the task ids handled."""
        if not self._housekeeping():
            return []
        return self._dispatch_due()

    def _housekeeping(self) -> bool:
        """The slow-cadence bookkeeping: heartbeat, orphan recovery, quest-folder/goal sync and the
        autopilot pass producer. Returns False when the client is unconfigured (nothing to do)."""
        if not self.client.configured:
            log.info("Quest key not configured — nothing to poll. Exiting cleanly.")
            return False
        # Heartbeat FIRST each cycle so the backend always knows the env is live + what it can do,
        # even on a scan that finds no due tasks. Best-effort, like progress-posting: a failed
        # heartbeat is logged and never blocks discovery/execution.
        self._emit_heartbeat()
        # Reconcile anything a PREVIOUS process claimed and never finished (it was killed). Runs
        # once per process, before discovery, so recovered work is eligible in this very scan.
        if not self._orphans_reconciled:
            self._orphans_reconciled = True
            self._recover_orphans()
        # Quest-folder periodic sync (opt-in, cfg.quest_folder_map) runs every scan regardless of
        # task pickup below — it's a light data sync, not new work, so it isn't gated by the
        # resource/token guards that protect against taking on MORE agentic work.
        self._sync_all_quest_folders()
        # Autopilot's own producer: guarantee the recurring pass task exists whenever a quest is
        # opted in. Like the folder sync above, this is light bookkeeping rather than agentic work,
        # so it runs before the resource/token gates that hold back task PICKUP.
        self._ensure_autopilot_pass()
        # Monthly bootstrap report (opt-in): send a report if the corpus is ready and a month
        # has passed since the last report, or if this is the first report. Like folder sync,
        # this is light bookkeeping not gated by resource/token limits.
        self._send_bootstrap_report_if_due()
        return True

    def _dispatch_due(self, pool: Optional[ThreadPoolExecutor] = None) -> List[str]:
        """Discover due tasks, claim them in priority order and run them.

        THE one dispatch path for every task (scheduled, delegated, real-time). Without ``pool``
        it runs the batch and waits for it (``poll --once``). With a long-lived ``pool`` it only
        submits as many tasks as there are free worker slots and returns at once, so the dispatch
        loop (``_dispatch_loop``) keeps picking up new work while earlier runs are still going."""
        # Resource gate BEFORE discovery/claiming: an overloaded host takes on NO new work this scan. Skipping
        # is lossless — unclaimed tasks stay queued and fire on a later scan once resources
        # recover (in_progress work is never touched).
        if self.resources.check():
            (log.debug if pool is not None else log.info)(
                "host overloaded — skipping task pickup this scan; queued tasks will run "
                "once resources recover")
            return []
        # Daily token budget gate: pause new pickup when the day's API token limit is exceeded.
        # Lossless for the same reason: unclaimed tasks stay queued and run on a later scan (or
        # the next UTC day once the counter resets at midnight).
        if self._usage_tracker and self._usage_tracker.over_limit():
            log.warning(
                "daily token limit reached (%s) — pausing task pickup until midnight UTC",
                self._usage_tracker.status(),
            )
            return []
        try:
            due = self.client.discover_due(
                now=datetime.now(timezone.utc), team_ids=self.discovery_team_ids(),
                env_id=self.cfg.env_id)
        except (QuestApiError, QuestNotConfigured) as e:
            (log.debug if pool is not None else log.info)(
                "discovery unavailable (%s) — will retry next scan", e)
            return []

        # The backend's due filter is date-granular, so it hands back tomorrow-morning's work as
        # soon as UTC rolls over. Keep only what the LOCAL clock says has actually arrived --
        # local to the RUNNER for an ordinary task, local to the QUEST's own run_timezone for a
        # per-quest autopilot pass (``_pass_timezone_for``; the schedule snapshot it reads was
        # already refreshed above, by ``_ensure_autopilot_pass``, before this call).
        due = [t for t in due if self._claimable_team(t)]
        due, deferred = _due_now_locally(due, tz_for=self._pass_timezone_for)
        if deferred:
            held = ", ".join(self._deferred_task_desc(t) for t in deferred)
            # The dispatch loop scans every few seconds: say it once per change, not per tick.
            if pool is None or held != getattr(self, "_last_held_log", None):
                self._last_held_log = held
                log.info("holding %d task(s) until their local scheduled time: %s",
                         len(deferred), held)

        fresh = [t for t in due if not self.state.seen(_task_signature(t))]
        # Priority, not a separate lane: a task a person is waiting on goes first, then oldest first.
        fresh.sort(key=lambda t: (not t.get("real_time"), str(t.get("created_at") or "")))
        if pool is not None:
            return self._submit_to_pool(pool, fresh)
        if not fresh:
            return []
        log.info("%d due task(s), %d new to handle", len(due), len(fresh))

        handled: List[str] = []
        workers = max(1, min(self.cfg.max_concurrent_tasks, len(fresh)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._handle_one_guarded, t): t for t in fresh}
            # Drive handling/logging in COMPLETION order (as_completed), not submission order —
            # so a fast task is reported as soon as it finishes instead of waiting behind a
            # slower task that happened to be submitted first.
            for fut in as_completed(futures):
                try:
                    tid = fut.result()
                    if tid:
                        handled.append(tid)
                except Exception as e:  # noqa: BLE001 — one bad task never kills the scan
                    log.error("task handling crashed: %s", e)
        return handled

    def _submit_to_pool(self, pool: ThreadPoolExecutor, fresh: List[Dict[str, Any]]) -> List[str]:
        """Claim and submit the highest-priority ``fresh`` tasks, up to the free worker slots.

        The rest are left untouched (still queued, not marked seen), so a later tick takes them
        as slots free up. The slot is claimed here, at submit time, so the next tick's own
        discovery cannot hand the same task out twice and the free-slot count stays honest."""
        submitted: List[str] = []
        now = time.monotonic()
        for task in fresh:
            task_id = str(task.get("id") or task.get("task_id") or "")
            # Oldest-first order means an unclaimable old task (404 on claim, owned by another
            # account) would take a slot every tick and starve everything behind it: that is how
            # the 2026-10-06 autopilot passes never ran. Skip it until its cooldown lapses.
            if self._claim_cooldown.get(task_id, 0.0) > now:
                continue
            with self._inflight_lock:
                if len(self._inflight) >= max(1, self.cfg.max_concurrent_tasks):
                    break
            if not self._claim_slot(task_id):
                continue
            pool.submit(self._run_claimed, task)
            submitted.append(task_id)
        if submitted:
            log.info("dispatching %d task(s): %s", len(submitted), ", ".join(submitted))
        return submitted

    def _run_claimed(self, task: Dict[str, Any]) -> Optional[str]:
        """Run a task whose in-process slot ``_submit_to_pool`` already claimed."""
        task_id = str(task.get("id") or task.get("task_id") or "")
        try:
            return self._handle_one(task)
        except Exception:  # noqa: BLE001 -- one bad task never kills the dispatch loop
            log.error("task %s crashed", task_id, exc_info=True)
            return None
        finally:
            self._release_slot(task_id)
            self.state.release_in_flight(task_id)

    def _dispatch_loop(self, stop_event: threading.Event) -> None:
        """Background thread: pick up due tasks every ``dispatch_interval_seconds`` (seconds, not
        the minutes-long housekeeping scan), so a new task starts almost at once."""
        interval = self.cfg.dispatch_interval_seconds
        pool = ThreadPoolExecutor(max_workers=max(1, self.cfg.max_concurrent_tasks),
                                  thread_name_prefix="qar-task")
        try:
            while not stop_event.is_set():
                try:
                    if self.client.configured:
                        with self._inflight_lock:
                            full = len(self._inflight) >= max(1, self.cfg.max_concurrent_tasks)
                        if not full:
                            self._dispatch_due(pool)
                except Exception:  # noqa: BLE001 -- the dispatch loop must never die
                    log.error("dispatch iteration failed", exc_info=True)
                if stop_event.wait(interval):
                    return
        finally:
            pool.shutdown(wait=False)

    def _recover_orphans(self) -> None:
        """Re-queue tasks a previous life of this runner claimed and never reported.

        A claim PATCHes the row to ``in_progress``; that is what stops a second worker taking it.
        If the worker then dies, the row still says someone is running it and nobody is. The only
        thing that eventually moves it is a backend staleness sweeper, hours later, and it moves it
        to ``failed`` -- which for an autopilot quest means the day produces no mail and no error,
        because failed rows are deliberately not mailable. The work is not slow, it is gone, and
        silently. This closes that gap from the side that actually knows: the runner remembers what
        it was holding (``StateStore.claim_in_flight``), so a fresh process can hand it back.

        Deliberately conservative. The row's CURRENT status is re-read first and only a still
        ``in_progress`` row is touched -- a task that actually finished (or that another
        environment has since taken) is left exactly as it is. Recovery is re-queueing rather than
        failing, because nothing here knows whether the work was impossible or merely interrupted;
        a queued row gets tried again, and a genuinely broken one fails on its own terms with a
        real reason. Orphans are consumed once (see ``take_orphans``), so an unrecoverable id
        cannot become a retry loop across restarts.
        """
        try:
            orphans = self.state.take_orphans()
        except Exception:  # noqa: BLE001 -- recovery must never stop a scan from happening
            log.warning("could not read abandoned claims from the state store", exc_info=True)
            return
        if not orphans:
            return
        log.warning("recovery: %d task(s) were claimed by a previous run that did not finish "
                    "(most likely it was killed) — reconciling: %s",
                    len(orphans), ", ".join(sorted(orphans)))
        for task_id, claimed_at in sorted(orphans.items()):
            try:
                current = self.client.get_task(task_id) or {}
                status = str((current.get("task") or current).get("status") or "").strip().lower()
                if status != "in_progress":
                    log.info("recovery: task %s is %r now, not the abandoned in_progress — "
                             "leaving it alone", task_id, status or "unknown")
                    continue
                self.client.update_task(task_id, {"status": "queued"})
                log.warning("recovery: task %s was abandoned in_progress (claimed %s) and has "
                            "been re-queued; it will run on a following scan", task_id, claimed_at)
            except Exception as e:  # noqa: BLE001 -- one bad id never blocks the others
                log.warning("recovery: could not reconcile abandoned task %s (%s) — it will be "
                            "left for the backend's own staleness sweep", task_id, e)

    def _requested_action_is_ours(self, task: Dict[str, Any], task_id: str) -> bool:
        """Whether this runner may claim a task queued for a published action: it must name this
        runner as publisher, and an environment-pinned task goes only to the runner of that
        environment (a runner with no environment never claims one). A task that fails either test
        is left unclaimed for the runner it belongs to."""
        from .action_catalog import PUBLISHER_ID
        request = task.get("requested_action") or {}
        if request.get("publisher") != PUBLISHER_ID:
            log.info("task %s asks for an action published by %r, not this runner -- not claiming",
                     task_id, request.get("publisher"))
            return False
        pinned = str(task.get("env_id") or request.get("env_id") or "")
        if pinned and pinned != str(self.cfg.env_id or ""):
            log.info("task %s is pinned to another environment -- not claiming", task_id)
            return False
        return True

    def _run_requested_action(self, task: Dict[str, Any], task_id: str) -> Optional[str]:
        """Run the single tool or connector operation a workflow step asked for and report the outcome."""
        from ..config import resolve_tool_registry
        from .requested_action import run_requested_action
        try:
            outcome = run_requested_action(task, resolve_tool_registry(self.cfg), self.cfg.mcp_servers, self.client)
        except Exception as e:  # noqa: BLE001 -- an action that crashes is a failed task, never a lost one
            log.error("requested action for task %s crashed", task_id, exc_info=True)
            outcome = None
            self.client.report_failed(task_id, f"The requested action crashed: {e}")
        if outcome is not None:
            if outcome.ok:
                self.client.report_done(task_id, outcome.text)
            else:
                self.client.report_failed(task_id, outcome.text)
        log.info("task %s -> requested action %s", task_id, "done" if outcome and outcome.ok else "failed")
        return task_id

    def publish_action_catalog(self) -> None:
        """Offer this runner's tools and connectors to Quest Workflows (see runner/action_catalog.py).
        Runs on the heartbeat; the publisher itself skips unchanged, recently sent catalogs."""
        from ..config import resolve_tool_registry
        from .action_catalog import CatalogPublisher, build_actions
        publisher = getattr(self, "catalog_publisher", None)
        if publisher is None:
            publisher = self.catalog_publisher = CatalogPublisher()
        targets = [("team", t) for t in self.configured_teams()]
        if self.cfg.org_id:
            targets.append(("org", self.cfg.org_id))
        if not targets:
            return
        actions = getattr(self, "catalog_actions", None)
        if actions is None or time.monotonic() - getattr(self, "catalog_built_at", 0.0) > 3600:
            actions = self.catalog_actions = build_actions(resolve_tool_registry(self.cfg), self.cfg.mcp_servers)
            self.catalog_built_at = time.monotonic()
        publisher.publish(self.client, targets, actions, env_id=self.cfg.env_id, label=self.cfg.runner_label)

    def attach_tool_listing(self) -> None:
        """Hand the client this runner's tool catalog (read-only listing) so the heartbeat reports it.
        Computed once; best-effort, a failure just means no tools are reported."""
        if getattr(self, "tool_listing_attached", False):
            return
        self.tool_listing_attached = True
        try:
            from ..config import resolve_tool_registry
            registry = resolve_tool_registry(self.cfg)
            if registry is not None:
                self.client.available_tools = registry.public_listing()
        except Exception:  # noqa: BLE001 -- reporting tools must never break a heartbeat
            log.info("tool listing not attached", exc_info=True)

    def _emit_heartbeat(self) -> None:
        """Best-effort env heartbeat: report this runner is live + its capabilities.

        Never raises — a heartbeat failure (no team_id/org_id, network, endpoint absent) is
        logged and the poll proceeds. The team_id, org_id, runner_label, and env_id come from the
        consumer's RunnerConfig (env_id distinguishes this runner when a team/org attaches
        several).

        When BOTH team_id and org_id are configured, this sends TWO independent heartbeats (team
        scope, then org scope) rather than picking one: `post_environment_heartbeat` treats org_id
        as scope-exclusive per call (org_id set -> org-only POST, team_id ignored for that call),
        so a single call can't register both. Dual-heartbeating keeps every existing team-scoped
        consumer (routing, fan-out, env pickers) working unchanged while ALSO keeping the org-level
        registration alive, so nothing regresses for a runner that opts into org-wide availability
        on top of its existing team. Each call is independently best-effort; one failing never
        blocks the other.

        A lane serving SEVERAL teams (``cfg.team_ids``) heartbeats to each of them -- the union of
        the home team and the discovered set, in a stable order, deduplicated. The heartbeat is
        how a team learns this environment exists and what it can do, so a team whose work this
        lane runs but which never hears from it shows the lane as absent and routes nothing to it.
        One POST per team, each independently best-effort for the same reason as above."""
        if not self.cfg.org_id and not self.cfg.team_id and not self.cfg.team_ids:
            return  # no team or org to attach the env to — nothing to heartbeat (still a valid poll)
        self.attach_tool_listing()
        for team in self.configured_teams():
            try:
                self.client.post_environment_heartbeat(
                    self._capabilities,
                    runner_label=self.cfg.runner_label,
                    env_id=self.cfg.env_id,
                    team_id=team,
                )
            except Exception as e:  # noqa: BLE001 — heartbeat is best-effort, never breaks the scan
                log.info("environment heartbeat (team %s) failed (%s) — continuing poll", team, e)
        if self.cfg.org_id:
            try:
                self.client.post_environment_heartbeat(
                    self._capabilities,
                    runner_label=self.cfg.runner_label,
                    env_id=self.cfg.env_id,
                    org_id=self.cfg.org_id,
                )
            except Exception as e:  # noqa: BLE001 — heartbeat is best-effort, never breaks the scan
                log.info("environment heartbeat (org scope) failed (%s) — continuing poll", e)
        try:
            self.publish_action_catalog()
        except Exception:  # noqa: BLE001 — publishing the catalog is best-effort, never breaks the scan
            log.info("action catalog publish failed — continuing poll", exc_info=True)

    def _deferred_task_desc(self, task: Dict[str, Any]) -> str:
        """One "holding N task(s)" line entry, naming the zone it was compared in when it had
        one (a per-quest autopilot pass), so an operator reading the log can tell a quest's own
        timezone apart from the runner's local clock without a debugger."""
        base = (f"{task.get('task_id') or task.get('id')}@{task.get('scheduled_date')} "
               f"{task.get('scheduled_time') or '00:00'}")
        zone = self._pass_timezone_for(task)
        return f"{base} ({zone})" if zone else base

    def _claim_slot(self, task_id: str) -> bool:
        """Reserve ``task_id`` for in-process handling. False if another path here already has it.

        See the ``_inflight`` docstring on ``__init__`` -- this only guards a race BETWEEN this
        process's own background scan and its fast lane, not across separate runner processes."""
        if not task_id:
            return True
        with self._inflight_lock:
            if task_id in self._inflight:
                return False
            self._inflight.add(task_id)
            return True

    def _release_slot(self, task_id: str) -> None:
        if not task_id:
            return
        with self._inflight_lock:
            self._inflight.discard(task_id)

    def _handle_one_guarded(self, task: Dict[str, Any]) -> Optional[str]:
        """Wrap ``_handle_one`` with the in-process claim guard (see ``_claim_slot``).

        Used by the background scan's ThreadPoolExecutor so it never handles a task the fast lane
        (wait channel / fallback poll) picked up in the same instant."""
        task_id = str(task.get("id") or task.get("task_id") or "")
        if not self._claim_slot(task_id):
            log.info("task %s already being handled by the fast lane -- skipping this scan", task_id)
            return None
        try:
            return self._handle_one(task)
        finally:
            self._release_slot(task_id)
            self.state.release_in_flight(task_id)

    def _handle_one(self, task: Dict[str, Any]) -> Optional[str]:
        # Context-request tasks never run the goal loop: a small, bounded, side-effect-free
        # local context assembly, reported back as fast as possible. Route them BEFORE the
        # resource/token-budget gates below -- they cost far less than a real task and exist
        # specifically to be fast, so they should never be deferred by host-load pickup gating.
        if task.get("context_request") is not None:
            return self._handle_context_request(task)
        sig = _task_signature(task)
        task_id = str(task.get("id") or task.get("task_id") or "")
        # LANE PAUSE: Claude Code is at its usage limit, so new work would only be refused (and,
        # before this, was then marked failed, the whole queue in seconds). Hold it until the reset
        # instead, visibly and server-enforced, without claiming it. A task its owner explicitly
        # released AFTER the pause began ("Start now") goes through: they may know the limit has
        # reset, and if it has not, the run is simply held again.
        paused_on = usage_limit.active_limit()
        if paused_on is not None:
            if self._released_during_pause(task, paused_on):
                usage_limit.clear(f"task {task_id} was started by its owner")
            else:
                self._hold_for_usage_limit(task_id, paused_on)
                return None
        # Re-check resources PER TASK: overload can begin mid-scan (earlier tasks in this very
        # batch may be what pushed the host over). Defer BEFORE marking/claiming, so the task is
        # re-discovered and runs on a later scan once resources recover.
        if self.resources.check():
            log.info("host overloaded — deferring task %s to a later scan", task_id)
            return None
        # Re-check daily token budget per-task: a task earlier in this batch may have pushed us over.
        if self._usage_tracker and self._usage_tracker.over_limit():
            log.info(
                "daily token limit reached mid-scan (%s) — deferring task %s",
                self._usage_tracker.status(), task_id,
            )
            return None
        # Resolve WHO will run this (the AI representation/skill) so we can stamp it on the claim:
        # the rep slug if a rep_sync_resolver maps this task to a skill dir, else the runner label.
        if task.get("requested_action") and not self._requested_action_is_ours(task, task_id):
            return None
        target = self._resolve_rep_target(task)
        handler = self._handler_label(target)
        # Claim FIRST: claim() now returns None on failure (already claimed by another worker, or a
        # transient API error) instead of an ambiguous {}. Only mark the signature handled AFTER a
        # successful claim, so a failed claim leaves the task un-marked and it is re-offered on a
        # later scan. Mark BEFORE running (not after) so a crash mid-run still doesn't cause a
        # re-fire loop — the backend claim (in_progress) is the second guard either way.
        if self.client.claim(task_id, handler=handler) is None:
            log.info("could not claim task %s — skipping (will be re-offered later)", task_id)
            self._claim_cooldown[task_id] = time.monotonic() + CLAIM_RETRY_COOLDOWN_SECONDS
            return None
        self._claim_cooldown.pop(task_id, None)
        self.state.mark(sig)
        # Persist WHAT WE ARE HOLDING before running it. The claim above has already PATCHed the
        # row to in_progress, so from here until a terminal report the backend believes this
        # runner is on it -- and if this process is killed (OOM, SIGKILL) nothing else will ever
        # notice. ``_recover_orphans`` reconciles this on the next start; see take_orphans().
        self.state.claim_in_flight(task_id)
        # A task queued for one published action (a Quest workflow step) is that action and nothing
        # else: no planner, no deep run. See runner/requested_action.py.
        if task.get("requested_action"):
            return self._run_requested_action(task, task_id)
        # Opt-in: refresh this rep's skill file from its Quest profile right before running, so the
        # spawned agent reflects the latest persona + learned corrections. Best-effort: a sync
        # failure is logged and the task still runs (it just uses the last-synced skill file).
        # The pre-run pull (when the direction calls for it) also yields the rep's per-run preamble,
        # so the deep run executes AS that rep with no extra consumer glue.
        # Fallback: when no rep is resolved for this task, the TASK DOCUMENT may carry its own
        # persona/system prompt in ``rep_preamble`` (see _task_rep_preamble). A resolved rep always
        # wins -- the task field only fills the gap.
        rep_preamble = self._pull_rep_for(task, target) or self._task_rep_preamble(task)
        # Opt-in: refresh the task's linked quest folder's QUEST_SYNC.md before running, when
        # cfg.quest_folder_map maps this task's goal/quest to a local folder.
        self._pull_quest_folder_for(task)
        executor = TaskExecutor(self.client, self._orch(),
                                quest_folder_map=getattr(self.cfg, "quest_folder_map", None),
                                autopilot_pass=self._autopilot,
                                quest_folder_zones=getattr(
                                    self.cfg, "quest_folder_zones", True),
                                update_engine=self._update_engine,
                                guidance_manager=_guidance_manager_of(self.cfg))
        outcome = executor.execute(task, rep_preamble=rep_preamble)
        log.info("task %s -> %s", task_id, outcome.status)
        # Opt-in push-back: after the run, write the local skill file back up to Quest when the
        # configured direction asks for it. Best-effort and AFTER the task is reported — a sync
        # failure here must never fail the task.
        self._push_rep_for(task, target)
        # Opt-in push-back: post any locally-queued notes on the quest folder up to Quest.
        self._push_quest_folder_for(task)
        # Opt-in: record this task's outcome into the rep's turn store so future runs can recall it.
        # A task paused on the Claude usage limit has no outcome yet; its resumed run records one.
        if getattr(outcome, "status", None) != "waiting":
            self._record_rep_turn(task, target, outcome)
        return task_id

    # --- the usage-limit lane pause (see core.usage_limit) -----------------------

    @staticmethod
    def _released_during_pause(task: Dict[str, Any], paused_on: "usage_limit.UsageLimit") -> bool:
        """Whether the task's owner pressed "Start now" after this pause began."""
        stamp = str(task.get("start_requested_at") or "").strip()
        if not stamp:
            return False
        parsed = _parse_dt(stamp)
        if parsed is None:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp() > paused_on.seen_at

    def _hold_for_usage_limit(self, task_id: str, paused_on: "usage_limit.UsageLimit") -> None:
        """Hold a discovered task until the pause ends: visible on the task, enforced by the backend."""
        resume_at = paused_on.resume_at()
        hold = getattr(self.client, "hold_for_usage_limit", None)
        if callable(hold) and task_id:
            hold(task_id, start_at=resume_at, detail=paused_on.message)
        log.info("Claude Code usage limit (%s): holding task %s until %s", paused_on.label(),
                 task_id, resume_at.isoformat())

    # --- D1: context-request fast path (no goal execution, no LLM plan loop) ------

    def _handle_context_request(self, task: Dict[str, Any]) -> Optional[str]:
        """Answer ONE ``context_request`` task: assemble context LOCALLY and report it.

        This is the counterpart to quest-backend's quest-context hub / LocalFetchReferenceResolver:
        when a live chat turn on another environment needs THIS runner's local context, the backend
        queues a task carrying a structured ``context_request`` ({query, user_id, quest_ids,
        visited, max_chars}) instead of a normal instruction. Answering it never runs the brain's
        plan/gather/replan loop or a deep run -- it just reuses the SAME ContextAssembler this
        runner already builds for its own chat/task turns (cards + vector search, all local/lexical,
        no extra LLM call beyond whatever the assembler itself already makes for consolidation), so
        it stays fast and side-effect-free.

        Bounded by the request's ``max_chars`` (soft; truncates with a marker like the hub's own
        merge budget). Reports ``done`` with the assembled text -- plus, when available, the
        assembler's ``card_metadata`` via ``report_done_with_data`` so the backend's hub can surface
        remote-env cards like any other card (see quest-backend's D4). Never raises to the caller:
        any failure here reports the task ``failed`` with the error so the backend's fan-out sees a
        clean terminal state instead of waiting out its own timeout budget."""
        task_id = str(task.get("id") or task.get("task_id") or "")
        cr = task.get("context_request") or {}
        query = str(cr.get("query") or task.get("text") or "").strip()
        max_chars = cr.get("max_chars")

        if self.client.claim(task_id, handler="context-request") is None:
            log.info("could not claim context-request task %s -- skipping", task_id)
            return None
        self.state.mark(_task_signature(task))

        if not query:
            self.client.report_done_with_data(task_id, "")
            return task_id
        try:
            orch = self._orch()
            assembler = getattr(orch, "context_assembler", None)
            if assembler is None:
                self.client.report_done_with_data(task_id, "")
                return task_id
            assembled = assembler.assemble(query)
            text = (getattr(assembled, "context_view", "") or "").strip()
            cards = list(getattr(assembled, "card_metadata", None) or [])
            if isinstance(max_chars, (int, float)) and max_chars > 0 and len(text) > max_chars:
                text = text[: int(max_chars)].rstrip() + "\n...[truncated]"
            self.client.report_done_with_data(
                task_id, text, {"card_metadata": cards} if cards else None)
            log.info("context-request %s answered (%d chars, %d card(s))",
                     task_id, len(text), len(cards))
        except Exception as e:  # noqa: BLE001 -- must still terminate the task cleanly
            log.error("context-request %s failed: %s", task_id, e, exc_info=True)
            self.client.report_failed(task_id, f"context assembly failed: {type(e).__name__}: {e}")
        return task_id

    # --- fast lane: real-time tasks, served faster than the background scan ----------------------

    def _claimable_team(self, task: Dict[str, Any]) -> bool:
        """False for a task on a team outside ``cfg.claim_teams`` (see that field). Teamless tasks
        and an empty allowlist always pass."""
        allowed = self.cfg.claim_teams
        team = task.get("team_id")
        if not allowed or not team or team in allowed:
            return True
        log.debug("skipping task %s: team %s is not in this lane's claim_teams",
                  task.get("task_id") or task.get("id"), team)
        return False

    def _dispatch_fast_task(self, task: Dict[str, Any]) -> None:
        """Claim-and-run ONE task delivered by the fast lane (wait channel or fallback poll).

        Eligibility for the fast lane is the backend's generic ``real_time`` flag, not a task type --
        any task-creation path (a live chat delegate, a context-request, or a future real-time-
        originated kind) can set it. So far every real_time task happens to be a context-request
        (see ``_handle_context_request``), and ``_handle_one`` already routes those without running
        the goal loop based on the presence of a ``context_request`` payload -- a separate,
        execution-routing decision, not an eligibility one. Anything else delivered here (a
        real_time task that is not a context-request) simply falls back to the normal execution path
        so the fast lane never silently drops unrecognized work. Deduped against the shared
        signature store and guarded against the background scan claiming the SAME task concurrently
        (see ``_claim_slot``)."""
        task_id = str(task.get("id") or task.get("task_id") or "")
        if not task_id or self.state.seen(_task_signature(task)) or not self._claimable_team(task):
            return
        if not self._claim_slot(task_id):
            return  # the background scan already has this one in flight
        try:
            self._handle_one(task)
        except Exception:  # noqa: BLE001 -- the fast lane must never die on a bad task
            log.error("fast lane: handling task %s crashed", task_id, exc_info=True)
        finally:
            self._release_slot(task_id)
            self.state.release_in_flight(task_id)

    def discovery_team_id(self) -> str:
        """The team scope BOTH discovery paths must use (background scan and fast lane).

        ``discovery_team_id`` when the consumer set one (allows owner-scoped discovery on a
        personal lane while ``team_id`` still routes heartbeat/escalation); otherwise fall back to
        ``team_id``. Returns "" for owner-scoped, which the client turns into "send no team filter".

        The fast lane MUST share this with the background scan. Scoping the real-time channel by
        ``cfg.team_id`` alone silently strands every owner-scoped task: the Quest UI creates a
        personal chat task with ``team_id=None``, so a team-filtered wait/poll matches nothing and
        the task falls through to the full ``poll_interval_seconds`` background scan -- which, in a
        live chat where someone is waiting on the reply, reads as the lane simply not picking it up.
        """
        return (self.cfg.discovery_team_id
                if self.cfg.discovery_team_id is not None
                else (self.cfg.team_id or ""))

    def configured_teams(self) -> List[str]:
        """The REAL teams this lane is attached to: its home team plus any discovery set, in a
        stable order, deduplicated, with empties dropped.

        Distinct from ``discovery_team_ids`` on purpose. That one answers "what scope do I ask the
        task queue for", and legitimately contains ``""`` (owner-scoped, meaning "send no team
        filter"). This one answers "which teams does this lane belong to" for the calls that need
        a NAMED team -- the environment heartbeat and the legacy team-wide pass sweep -- where ""
        is not a team and an owner-scoped discovery override must not change which team the lane
        registers with. On a single-team lane this is exactly ``[cfg.team_id]``, which is what
        both of those call sites used before this existed.
        """
        teams: List[str] = []
        for team in [self.cfg.team_id] + list(self.cfg.team_ids):
            if team and team not in teams:
                teams.append(team)
        return teams

    def discovery_team_ids(self) -> List[str]:
        """The full SET of teams both discovery paths serve, for a lane that serves several.

        ``cfg.team_ids`` when the consumer set one, otherwise the single scope
        ``discovery_team_id()`` already returns, as a one-element list. That one element may be
        ``""`` (owner-scoped), which the client turns into "send no team filter" exactly as
        before -- a single-team lane's request is therefore unchanged in every case.

        ``discovery_team_id`` WINS over ``team_ids`` when the consumer set it at all, including an
        explicit ``""``. The two are a contradiction (one says "this scope only", the other says
        "all of these"), and honouring the narrower, older knob is what guarantees no existing
        deployment changes behaviour by adding this field.

        One list, one request: the whole set goes into a single call rather than a call per team,
        which matters most for ``wait_for_interactive`` -- see its docstring.
        """
        if self.cfg.discovery_team_id is None and self.cfg.team_ids:
            return list(self.cfg.team_ids)
        return [self.discovery_team_id()]

    def _fast_lane_loop(self, stop_event: threading.Event) -> None:
        """Background thread: serve REAL-TIME work with sub-poll-interval latency (D2 revised).

        Two strategies, chosen by config:
          * ``wait_channel_enabled`` (default) -- hold a long-poll GET (blocks server-side up to
            ``wait_timeout_seconds``) so a live chat context-request is answered close to instantly;
            the connection is reopened immediately after each return (empty or not) -- the long-poll
            itself provides the pacing, no extra sleep needed on the happy path.
          * disabled -- fall back to a short interval poll (``context_poll_seconds``) over just the
            real-time queue. ``context_poll_seconds <= 0`` disables the fast lane entirely (the
            background scan's ``poll_interval_seconds`` is then the only cadence, exactly the
            pre-fast-lane behavior).

        A wait call that fails fast (well under its requested timeout -- unconfigured client,
        network error, or an older backend without the ``/wait`` endpoint) is treated as trouble,
        not a clean empty wait, and backs off briefly before retrying so a broken endpoint can never
        turn into a tight retry loop. Never raises: one bad iteration is logged and the loop
        continues, exactly like the background scan's own error handling."""
        import time as _time

        if not self.client.configured:
            return  # nothing to attach the fast lane to
        if (not self.cfg.team_id and self.cfg.discovery_team_id is None
                and not self.cfg.team_ids):
            return  # no team AND no explicit owner-scoped discovery -- nothing to poll for

        while not stop_event.is_set():
            try:
                if self.cfg.wait_channel_enabled:
                    started = _time.monotonic()
                    # The WHOLE team set in ONE long-poll, never one call per team: this endpoint
                    # hands back a single FIFO-oldest task for the scope it was given, so a lane
                    # that asked per team and filtered the rest would discard work and reconnect
                    # in a tight loop for as long as it sat at the head of the queue.
                    task = self.client.wait_for_interactive(
                        team_ids=self.discovery_team_ids(), env_id=self.cfg.env_id,
                        timeout=self.cfg.wait_timeout_seconds,
                    )
                    elapsed = _time.monotonic() - started
                    if task:
                        self._dispatch_fast_task(task)
                    elif elapsed < 1.0:
                        # Looks like a fast failure, not a clean ~timeout-length empty wait.
                        if stop_event.wait(min(5.0, max(1.0, self.cfg.context_poll_seconds))):
                            return
                    # else: a normal empty wait -- reconnect immediately, no sleep.
                else:
                    interval = self.cfg.context_poll_seconds
                    if interval <= 0:
                        return  # fast lane explicitly disabled
                    for t in self.client.list_interactive_due(
                        team_ids=self.discovery_team_ids(), env_id=self.cfg.env_id,
                    ):
                        self._dispatch_fast_task(t)
                    if stop_event.wait(interval):
                        return
            except Exception:  # noqa: BLE001 -- the fast lane must never die
                log.error("fast lane iteration failed", exc_info=True)
                if stop_event.wait(1.0):
                    return

    # --- run modes -----------------------------------------------------------

    def run_forever(self, *, stop_event: Optional[threading.Event] = None):
        import time

        # The fast lane runs in its OWN daemon thread for the life of the service, independent of
        # the background scan's stop_event contract below: it gets its own internal Event so it can
        # be stopped deterministically in tests (via the returned thread) while still shutting down
        # automatically at process exit in production (daemon=True) even when run_forever() itself
        # never returns (the systemd/cron entry point calls it with no stop_event).
        fast_stop = threading.Event()
        fast_thread = threading.Thread(
            target=self._fast_lane_loop, args=(fast_stop,),
            name="qar-fast-lane", daemon=True,
        )
        fast_thread.start()

        # ONE dispatch path at a short cadence; the long scan below then only does housekeeping.
        # dispatch_interval_seconds <= 0 keeps the old behaviour (the scan dispatches too).
        dispatch_thread = None
        dispatch_stop = threading.Event()
        split = self.cfg.dispatch_interval_seconds > 0
        if split:
            dispatch_thread = threading.Thread(
                target=self._dispatch_loop, args=(dispatch_stop,),
                name="qar-dispatch", daemon=True)
            dispatch_thread.start()

        interval = self.cfg.poll_interval_seconds
        try:
            while True:
                # While the host is overloaded, wait at the guard's (shorter) re-check cadence rather
                # than burning full poll cycles — so the lane RESUMES promptly when resources recover.
                if not self.resources.wait_until_ok(stop_event=stop_event):
                    return  # stopped while paused
                try:
                    if split:
                        self._housekeeping()
                    else:
                        self.run_once()
                except Exception as e:  # noqa: BLE001 — a transient error must not kill the loop
                    log.error("scan failed: %s", e)
                if stop_event is not None and stop_event.wait(interval):
                    return
                if stop_event is None:
                    time.sleep(interval)
        finally:
            fast_stop.set()
            dispatch_stop.set()
