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

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import RunnerConfig, build_orchestrator, derive_capabilities
from ..resources import ResourceGuard, ResourceLimits
from .autopilot import AUTOPILOT_PASS_KIND, OPEN_TASK_STATUSES, AutopilotPass
from .executor import TaskExecutor
from .quest_client import QuestApiError, QuestClient, QuestDecisionSink, QuestNotConfigured
# StateStore lives in its own module (runner/state_store.py) so the channel-runner lane can reuse
# the SAME dedup mechanism without duplicating it. Re-exported here (not just imported for local
# use) so `from quest_ai_runner.runner.poller import StateStore` keeps working unchanged.
from .state_store import StateStore

__all__ = ["Poller", "StateStore"]

log = logging.getLogger("quest-ai-runner.poller")


def _task_signature(task: Dict[str, Any]) -> str:
    """A stable per-task signature so each task fires exactly once.

    Includes status + an updated/scheduled marker so a re-queued or rescheduled task is treated
    as new, but the same queued task seen twice is deduped.
    """
    tid = task.get("id") or task.get("task_id") or ""
    marker = task.get("updated_at") or task.get("scheduled_time") or task.get("scheduled_date") or ""
    return f"{tid}:{task.get('status', 'queued')}:{marker}"


def _due_now_locally(tasks: List[Dict[str, Any]],
                     now: Optional[datetime] = None) -> tuple[List[Dict[str, Any]],
                                                              List[Dict[str, Any]]]:
    """Split discovered tasks into (due, not-yet-due) by LOCAL wall clock.

    Discovery asks the backend for `due_before=<ISO now, UTC>`, but the backend compares only the
    DATE portion of that timestamp against `scheduled_date` and never looks at `scheduled_time`
    (see quest-backend `assistant_task_storage.list_tasks`). Its answer is therefore a SUPERSET:
    correct to the day, silent about the hour. West of UTC that superset opens early -- a task set
    for 06:30 becomes "due" the moment UTC midnight passes, which is 17:00 the PREVIOUS afternoon
    in US/Pacific. A daily morning brief then runs the evening before, is written against the wrong
    day, and burns the occurrence its real slot needed. (Seen 2026-08-12/13 on the personal lane:
    a 06:30 brief ran at 17:17 the day before, twice.)

    So narrow the superset here, where the runner knows the wall clock the schedule was written
    against. A task is due once local `scheduled_date` + `scheduled_time` has arrived; a missing
    time means midnight, and an unscheduled task ("do it now", the chat-delegated case) is always
    due. Holding one back is lossless: it stays `queued` and surfaces on a later scan.

    Timezone: the runner's own local time, which is the tz a person authoring "06:30" means. The
    task model carries no tz of its own, so a runner in a different tz than the schedule's author
    is a real (pre-existing) limitation, not one this filter introduces.
    """
    now = now or datetime.now()
    due: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    for task in tasks:
        date = str(task.get("scheduled_date") or "").strip()
        if not date:
            due.append(task)
            continue
        clock = str(task.get("scheduled_time") or "00:00").strip()[:5]
        try:
            scheduled = datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M")
        except ValueError:
            # An unparseable schedule must never strand a task: fall back to the backend's answer.
            log.warning("task %s has an unreadable schedule (%r %r) — treating it as due",
                        task.get("task_id") or task.get("id"), date, clock)
            due.append(task)
            continue
        (due if scheduled <= now else deferred).append(task)
    return due, deferred


class Poller:
    def __init__(self, config: RunnerConfig, *, state_path: Optional[str] = None,
                 client: Optional[QuestClient] = None,
                 resource_guard: Optional[ResourceGuard] = None):
        self.cfg = config
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
                self.client, default_assignee_user_id=config.default_assignee_user_id)
        self.state = StateStore(state_path)
        self._orchestrator = None  # built lazily so an unconfigured poll degrades cleanly
        # Autopilot: built once (stateless other than the injected client/config) and handed to
        # every TaskExecutor this poller builds, so a task with ``handler == "autopilot"`` routes
        # to it instead of a deep run. Inert unless such a task is ever discovered.
        self._autopilot = AutopilotPass(
            self.client,
            team_id=config.team_id or "",
            persona_resolver=config.autopilot_persona_resolver,
            daily_budget=config.autopilot_daily_budget,
            adopt_recurring_default=config.autopilot_adopt_recurring,
            # Same map the folder sync uses, so a quest whose folder is already synced also gets
            # its canonical next-steps artifact read and refreshed by each pass.
            quest_folder_map=config.quest_folder_map,
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

    def _orch(self):
        if self._orchestrator is None:
            self._orchestrator = build_orchestrator(self.cfg)
        return self._orchestrator

    # --- one scan ------------------------------------------------------------

    def run_once(self) -> List[str]:
        """One discover -> claim -> run -> report pass. Returns the task ids handled this scan."""
        if not self.client.configured:
            log.info("Quest key not configured — nothing to poll. Exiting cleanly.")
            return []
        # Heartbeat FIRST each cycle so the backend always knows the env is live + what it can do,
        # even on a scan that finds no due tasks. Best-effort, like progress-posting: a failed
        # heartbeat is logged and never blocks discovery/execution.
        self._emit_heartbeat()
        # Quest-folder periodic sync (opt-in, cfg.quest_folder_map) runs every scan regardless of
        # task pickup below — it's a light data sync, not new work, so it isn't gated by the
        # resource/token guards that protect against taking on MORE agentic work.
        self._sync_all_quest_folders()
        # Autopilot's own producer: guarantee the recurring pass task exists whenever a quest is
        # opted in. Like the folder sync above, this is light bookkeeping rather than agentic work,
        # so it runs before the resource/token gates that hold back task PICKUP.
        self._ensure_autopilot_pass()
        # Resource gate AFTER the heartbeat (the backend should still see the env as live) but
        # BEFORE discovery/claiming: an overloaded host takes on NO new work this scan. Skipping
        # is lossless — unclaimed tasks stay queued and fire on a later scan once resources
        # recover (in_progress work is never touched).
        if self.resources.check():
            log.info("host overloaded — skipping task pickup this scan; queued tasks will run "
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
            # Use discovery_team_id when set (allows owner-scoped discovery on a personal lane while
            # still using team_id for heartbeat/escalation); otherwise fall back to team_id.
            disc_tid = (self.cfg.discovery_team_id
                        if self.cfg.discovery_team_id is not None
                        else (self.cfg.team_id or ""))
            due = self.client.discover_due(
                now=datetime.now(timezone.utc), team_id=disc_tid, env_id=self.cfg.env_id)
        except (QuestApiError, QuestNotConfigured) as e:
            log.info("discovery unavailable (%s) — will retry next scan", e)
            return []

        # The backend's due filter is date-granular, so it hands back tomorrow-morning's work as
        # soon as UTC rolls over. Keep only what the LOCAL clock says has actually arrived.
        due, deferred = _due_now_locally(due)
        if deferred:
            log.info("holding %d task(s) until their local scheduled time: %s", len(deferred),
                     ", ".join(f"{t.get('task_id') or t.get('id')}@{t.get('scheduled_date')} "
                               f"{t.get('scheduled_time') or '00:00'}" for t in deferred))

        fresh = [t for t in due if not self.state.seen(_task_signature(t))]
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

    def _emit_heartbeat(self) -> None:
        """Best-effort env heartbeat: report this runner is live + its capabilities.

        Never raises — a heartbeat failure (no team_id, network, endpoint absent) is logged and
        the poll proceeds. The team_id, runner_label, and env_id come from the consumer's
        RunnerConfig (env_id distinguishes this runner when a team attaches several)."""
        if not self.cfg.team_id:
            return  # no team to attach the env to — nothing to heartbeat (still a valid poll)
        try:
            self.client.post_environment_heartbeat(
                self._capabilities,
                runner_label=self.cfg.runner_label,
                env_id=self.cfg.env_id,
                team_id=self.cfg.team_id,
            )
        except Exception as e:  # noqa: BLE001 — heartbeat is best-effort, never breaks the scan
            log.info("environment heartbeat failed (%s) — continuing poll", e)

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

    def _handle_one(self, task: Dict[str, Any]) -> Optional[str]:
        # Context-request tasks never run the goal loop: a small, bounded, side-effect-free
        # local context assembly, reported back as fast as possible. Route them BEFORE the
        # resource/token-budget gates below -- they cost far less than a real task and exist
        # specifically to be fast, so they should never be deferred by host-load pickup gating.
        if task.get("context_request") is not None:
            return self._handle_context_request(task)
        sig = _task_signature(task)
        task_id = str(task.get("id") or task.get("task_id") or "")
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
        target = self._resolve_rep_target(task)
        handler = self._handler_label(target)
        # Claim FIRST: claim() now returns None on failure (already claimed by another worker, or a
        # transient API error) instead of an ambiguous {}. Only mark the signature handled AFTER a
        # successful claim, so a failed claim leaves the task un-marked and it is re-offered on a
        # later scan. Mark BEFORE running (not after) so a crash mid-run still doesn't cause a
        # re-fire loop — the backend claim (in_progress) is the second guard either way.
        if self.client.claim(task_id, handler=handler) is None:
            log.info("could not claim task %s — skipping (will be re-offered later)", task_id)
            return None
        self.state.mark(sig)
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
                                autopilot_pass=self._autopilot)
        outcome = executor.execute(task, rep_preamble=rep_preamble)
        log.info("task %s -> %s", task_id, outcome.status)
        # Opt-in push-back: after the run, write the local skill file back up to Quest when the
        # configured direction asks for it. Best-effort and AFTER the task is reported — a sync
        # failure here must never fail the task.
        self._push_rep_for(task, target)
        # Opt-in push-back: post any locally-queued notes on the quest folder up to Quest.
        self._push_quest_folder_for(task)
        # Opt-in: record this task's outcome into the rep's turn store so future runs can recall it.
        self._record_rep_turn(task, target, outcome)
        return task_id

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

    def _resolve_rep_target(self, task: Dict[str, Any]) -> Optional[tuple]:
        """Map a task to ``(user_id, skill_dir)`` via the opt-in ``rep_sync_resolver`` (or None).

        Resolved ONCE per task and reused for both the handler label (stamped on claim) and the
        rep skill-file sync. Never raises — a missing resolver or a bad one yields None."""
        resolver = getattr(self.cfg, "rep_sync_resolver", None)
        if resolver is None:
            return None
        try:
            return resolver(task)
        except Exception as e:  # noqa: BLE001 — a bad resolver must never break execution
            log.info("rep_sync_resolver raised (%s) — skipping rep resolution", e)
            return None

    def _handler_label(self, target: Optional[tuple]) -> Optional[str]:
        """Derive the handler label stamped on claim — WHO ran this task.

        If the rep resolver mapped the task to a ``(user_id, skill_dir)``, the handler is the
        basename of ``skill_dir`` (the rep slug, e.g. "alex"/"sam"). Otherwise fall back to
        the runner's configured ``runner_label`` (or None when neither is available)."""
        if target:
            _user_id, skill_dir = target
            slug = Path(str(skill_dir)).name
            if slug:
                return slug
        return self.cfg.runner_label or None

    def _rep_context_dirs(self, user_id: str) -> tuple:
        """Return ``(cards_dir, rep_notes_dir, rep_turns_dir)`` for a given rep.

        Deterministic from ``(user_id, cfg)`` so callers can reconstruct it cheaply without
        storing extra state.
        """
        import os as _os
        root = self.cfg.corpus_root or _os.getcwd()
        cards_dir = self.cfg.context_cards_dir or _os.path.join(root, ".quest-context")
        rep_notes_dir = _os.path.join(cards_dir, "reps", user_id, "notes")
        rep_turns_dir = _os.path.join(cards_dir, "reps", user_id, "turns")
        return cards_dir, rep_notes_dir, rep_turns_dir

    @staticmethod
    def _task_rep_preamble(task: Dict[str, Any]) -> Optional[str]:
        """The persona a TASK DOCUMENT supplies for itself: its optional ``rep_preamble`` field.

        A consumer that already knows the voice a task must speak in can stamp it on the task when
        it queues the work, and the runner will use it as the deep run's persona (and therefore as
        the voice of the fold-back "done" report) with no rep profile and no resolver wired. The
        motivating case is a task deferred out of a live conversation: the queueing side stamps that
        conversation's own system prompt on the task, so the report that lands back in the
        conversation sounds like the replies already in it.

        This is a FALLBACK only: when a rep IS resolved for the task, that rep's pulled persona wins
        (see ``_handle_one``). Anything that is not a non-empty string is ignored, so a malformed or
        placeholder field can never poison the run.
        """
        value = task.get("rep_preamble")
        if isinstance(value, str) and value.strip():
            return value
        return None

    def _pull_rep_for(self, task: Dict[str, Any], target: Optional[tuple] = None) -> Optional[str]:
        """Best-effort PRE-run pull, gated on direction; returns the rep's per-run preamble or None.

        Only fires when the consumer wired a ``rep_sync_resolver`` (OFF by default) AND the
        configured ``rep_sync_direction`` includes a pull ("pull" or "both"). ``target`` is the
        already-resolved ``(user_id, skill_dir)`` (or None to skip). We ``pull_rep_to_skill`` so
        the local skill file reflects the current persona/corrections, then read its MANAGED
        sections and compose them with the runner's context doctrine into a per-run preamble the
        executor injects into the deep run — so the task runs AS that rep by default, no extra
        consumer glue.

        Also builds rep-specific NoteContextStore and TurnContextStore instances, syncs the
        note store from the just-pulled profile, assembles both into context blocks, and appends
        them to the preamble.

        Never raises: a sync failure is logged and the run proceeds (with the previously synced
        file, and no preamble from this pull)."""
        if not target:
            return None
        if self.cfg.rep_sync_direction not in ("pull", "both"):
            return None  # direction is push-only: do not pull before the run
        user_id, skill_dir = target
        team_id = task.get("team_id") or self.cfg.team_id
        try:
            from pathlib import Path as _Path

            from ..core.context_doctrine import compose_deep_preamble
            from ..core.note_context_store import NoteContextStore
            from ..core.turn_context_store import TurnContextStore
            from .rep_sync import SKILL_FILE_NAME, parse_skill_file, pull_rep_to_skill

            _cards_dir, rep_notes_dir, rep_turns_dir = self._rep_context_dirs(user_id)

            # Build the note store and pass it to pull so the sync happens in one call.
            note_store = NoteContextStore(rep_notes_dir)
            pull_rep_to_skill(self.client, team_id, user_id, skill_dir, note_store=note_store)

            # Read the rep's persona + learned corrections back out of the just-pulled file.
            skill_text = (_Path(skill_dir) / SKILL_FILE_NAME).read_text(encoding="utf-8")

            # Assemble rep-specific note and turn context for the preamble.
            task_text = task.get("text") or task.get("title") or ""
            note_ctx = note_store.assemble(task_text)
            rep_turn_store = TurnContextStore(turns_dir=rep_turns_dir)
            turn_ctx = rep_turn_store.assemble(task_text)

            return self._build_rep_preamble(
                skill_text, compose_deep_preamble, parse_skill_file,
                note_ctx_view=note_ctx.context_view,
                turn_ctx_view=turn_ctx.context_view,
            )
        except Exception as e:  # noqa: BLE001 — best-effort, like progress posting/heartbeat
            log.info("rep pull for %s failed (%s) — running with existing skill file", user_id, e)
            return None

    @staticmethod
    def _build_rep_preamble(skill_text: str, compose_deep_preamble, parse_skill_file,
                            *, note_ctx_view: str = "", turn_ctx_view: str = "") -> Optional[str]:
        """Compose a deep-run preamble from a skill file's MANAGED sections (persona + learned).

        Generic: it only knows ``persona`` + ``learned_notes`` (the rep_sync managed shape) and the
        runner's context doctrine. Optionally appends rep-specific note context (learned corrections
        from the NoteContextStore) and turn context (past task history from the TurnContextStore).
        Returns None when the file carries no rep identity to inject."""
        parsed = parse_skill_file(skill_text or "")
        persona = (parsed.get("persona") or "").strip()
        learned = parsed.get("learned_notes") or []
        if not persona and not learned and not note_ctx_view and not turn_ctx_view:
            return None
        parts: List[str] = []
        if persona:
            parts.append("=== ACT AS THIS PERSON (their persona) ===\n" + persona)
        if learned:
            bullets = "\n".join(f"- {str(n.get('text', '')).strip()}"
                                for n in learned if str(n.get("text", "")).strip())
            if bullets:
                parts.append("=== LEARNED CORRECTIONS (apply these) ===\n" + bullets)
        if note_ctx_view:
            parts.append(note_ctx_view)
        if turn_ctx_view:
            parts.append(turn_ctx_view)
        if not parts:
            return None
        # Combine the runner's doctrine with this rep's persona/learned via the existing composer,
        # so deep agents obey the same disciplines AND adopt the rep's identity.
        return compose_deep_preamble("\n\n".join(parts))

    def _push_rep_for(self, task: Dict[str, Any], target: Optional[tuple] = None) -> None:
        """Best-effort POST-run push, gated on direction.

        Fires only when a ``rep_sync_resolver`` resolved a target AND ``rep_sync_direction`` is
        "push" or "both": the rep's local skill file is written back up to its Quest profile after
        the task ran. Never raises — a push failure is logged and the (already reported) task is
        unaffected."""
        if not target:
            return
        if self.cfg.rep_sync_direction not in ("push", "both"):
            return
        user_id, skill_dir = target
        team_id = task.get("team_id") or self.cfg.team_id
        try:
            from .rep_sync import push_skill_to_rep
            push_skill_to_rep(self.client, team_id, user_id, skill_dir)
        except Exception as e:  # noqa: BLE001 — best-effort; a push failure never fails the task
            log.info("rep push for %s failed (%s) — leaving Quest profile unchanged", user_id, e)

    def _record_rep_turn(self, task: Dict[str, Any], target: Optional[tuple],
                         outcome: Any) -> None:
        """Best-effort POST-run: record this task's outcome into the rep's per-rep turn store.

        Only fires when a rep resolver returned a target.  The rep's TurnContextStore lives at
        ``<cards_dir>/reps/<user_id>/turns/`` so it stays namespaced per rep and doesn't pollute
        the org-wide turn store.  Never raises."""
        if not target:
            return
        user_id, _skill_dir = target
        try:
            from ..core.turn_context_store import TurnContextStore
            _cards_dir, _rep_notes_dir, rep_turns_dir = self._rep_context_dirs(user_id)
            task_text = task.get("text") or task.get("title") or ""
            result_text = (getattr(outcome, "result", None) or "").strip()
            rep_turn_store = TurnContextStore(turns_dir=rep_turns_dir)
            rep_turn_store.record(task_text, {"response": result_text})
        except Exception as e:  # noqa: BLE001 — best-effort; never fails the task
            log.info("rep turn record for %s failed (%s) — continuing", user_id, e)

    # --- quest <-> local folder sync (opt-in, keyed off cfg.quest_folder_map) --

    def _quest_folder_for(self, task: Dict[str, Any]) -> Optional[tuple]:
        """Return ``(quest_id, folder)`` when this task's goal/quest is in ``cfg.quest_folder_map``.

        Unlike the rep resolver, no callable is needed: the task already carries the exact id
        (``goal_id`` for a personal "goal is the hub" quest, else ``quest_id``) that the map is
        keyed on. Returns None when unconfigured or the task's quest isn't mapped."""
        folder_map = getattr(self.cfg, "quest_folder_map", None)
        if not folder_map:
            return None
        qid = task.get("goal_id") or task.get("quest_id")
        if not qid:
            return None
        folder = folder_map.get(str(qid))
        return (str(qid), folder) if folder else None

    def _ensure_autopilot_pass(self) -> None:
        """Make a quest's autopilot opt-in actually DO something, by guaranteeing the recurring
        "Autopilot pass" task exists.

        Autopilot is deliberately implemented as a task rather than a daemon (no cron, pausable
        from the same UI, auditable in the same activity stream). The gap was that NOTHING created
        that task: opting a quest into Suggest/Act saved the setting and produced silence forever.
        This closes the loop from the runner side -- the lane that would execute the pass is the
        one that ensures it exists.

        Cost is one list call on a healthy scan. The expensive opt-in check (a state read per
        team quest) runs ONLY when no open pass task was found, so the steady state stays cheap.

        Best-effort throughout: any failure is logged and retried next scan. A missing pass task
        is a degraded feature, never a reason to skip the ordinary task pickup that follows.
        """
        if not self.cfg.autopilot_ensure_pass_task:
            return
        if not self.client.configured or not self.cfg.team_id:
            return
        try:
            existing = self.client.list_tasks(
                team_id=self.cfg.team_id, task_kind=AUTOPILOT_PASS_KIND)
            # A recurring series always has exactly one occurrence outstanding: the backend spawns
            # the next one when the current reaches a terminal status. So "an open occurrence
            # exists" is the correct liveness test for the whole series. Only if the series was
            # cancelled or never created does nothing open remain -- and then we make one.
            if any(str(t.get("status", "")).strip().lower() in OPEN_TASK_STATUSES
                   for t in existing):
                return
            if not self._any_quest_on_autopilot():
                return
            created = self.client.create_task(
                "Autopilot pass: scan this team's opted-in quests and make progress on their "
                "current-scope goals.",
                title="Autopilot pass",
                team_id=self.cfg.team_id,
                source="chat",
                task_kind=AUTOPILOT_PASS_KIND,
                recurrence={"frequency": "daily", "time": self.cfg.autopilot_pass_time},
                scheduled_time=self.cfg.autopilot_pass_time,
                env_id=self.cfg.env_id or None,
            ) or {}
            log.info("autopilot: created the recurring pass task %s (daily at %s)",
                     created.get("id") or created.get("task_id"), self.cfg.autopilot_pass_time)
        except Exception as e:  # noqa: BLE001 -- never let this block the scan
            log.warning("autopilot: could not ensure the recurring pass task (%s) — "
                        "will retry next scan", e)

    def _any_quest_on_autopilot(self) -> bool:
        """Whether ANY of the team's quests is opted in (``autopilot.mode`` in suggest/act).

        Mirrors ``AutopilotPass._eligible_quests``' two-read shape, and for the same reason: the
        team quest LISTING does not carry the ``autopilot`` block, so the mode has to be read off
        each quest's full state or every quest looks switched off.
        """
        quests = self.client.list_quests(team_id=self.cfg.team_id or None) or []
        for row in quests:
            quest_id = str(row.get("quest_id") or row.get("id") or "")
            if not quest_id:
                continue
            try:
                state = self.client.get_quest_autopilot(quest_id) or {}
            except Exception:  # noqa: BLE001 -- one unreadable quest never decides the answer
                continue
            if str((state.get("autopilot") or {}).get("mode") or "off") in ("suggest", "act"):
                return True
        return False

    def _sync_all_quest_folders(self) -> None:
        """Best-effort: sync EVERY entry in ``cfg.quest_folder_map``, independent of whether a
        task for that quest happens to be due this scan.

        The task-scoped hooks above (``_pull_quest_folder_for``/``_push_quest_folder_for``) only
        fire around a task that carries a mapped goal/quest id, so a folder whose quest never gets
        a task queued against it (e.g. someone just edits it on Quest directly) would otherwise
        never refresh. Calling this once per scan, from ``run_once()``, means every standing
        ``poll``/``run_forever`` process — the normal systemd/cron deployment — keeps every mapped
        folder current on its own poll cadence, with no task required. A per-entry failure (bad
        folder, API error) is logged and never blocks the other entries or the scan itself."""
        folder_map = getattr(self.cfg, "quest_folder_map", None)
        if not folder_map:
            return
        direction = self.cfg.quest_folder_sync_direction
        from .quest_folder_sync import sync_quest_folder
        for quest_id, folder in folder_map.items():
            try:
                sync_quest_folder(self.client, quest_id, folder, direction=direction)
            except Exception as e:  # noqa: BLE001 — one bad folder must not block the others/scan
                log.info("quest-folder periodic sync for %s failed (%s) — will retry next scan",
                         quest_id, e)

    def _pull_quest_folder_for(self, task: Dict[str, Any]) -> None:
        """Best-effort PRE-run pull: refresh the mapped folder's QUEST_SYNC.md from Quest.

        Fires only when the task's goal/quest resolves via ``cfg.quest_folder_map`` AND
        ``cfg.quest_folder_sync_direction`` includes a pull ("pull" or "both"). Never raises — a
        sync failure is logged and the run proceeds with whatever was last synced."""
        target = self._quest_folder_for(task)
        if not target:
            return
        if self.cfg.quest_folder_sync_direction not in ("pull", "both"):
            return
        quest_id, folder = target
        try:
            from .quest_folder_sync import pull_quest_to_folder
            pull_quest_to_folder(self.client, quest_id, folder)
        except Exception as e:  # noqa: BLE001 — best-effort, like the rep pull
            log.info("quest-folder pull for %s failed (%s) — folder left as last synced",
                     quest_id, e)

    def _push_quest_folder_for(self, task: Dict[str, Any]) -> None:
        """Best-effort POST-run push: post any locally-queued notes up to the mapped quest.

        Fires only when the task's goal/quest resolves via ``cfg.quest_folder_map`` AND
        ``cfg.quest_folder_sync_direction`` is "push" or "both". Never raises."""
        target = self._quest_folder_for(task)
        if not target:
            return
        if self.cfg.quest_folder_sync_direction not in ("push", "both"):
            return
        quest_id, folder = target
        try:
            from .quest_folder_sync import push_folder_to_quest
            push_folder_to_quest(self.client, quest_id, folder)
        except Exception as e:  # noqa: BLE001 — best-effort; a push failure never fails the task
            log.info("quest-folder push for %s failed (%s) — leaving Quest notes unchanged",
                     quest_id, e)

    # --- fast lane: real-time tasks, served faster than the background scan ----------------------

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
        if not task_id or self.state.seen(_task_signature(task)):
            return
        if not self._claim_slot(task_id):
            return  # the background scan already has this one in flight
        try:
            self._handle_one(task)
        except Exception:  # noqa: BLE001 -- the fast lane must never die on a bad task
            log.error("fast lane: handling task %s crashed", task_id, exc_info=True)
        finally:
            self._release_slot(task_id)

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

        if not self.client.configured or not self.cfg.team_id:
            return  # nothing to attach the fast lane to (mirrors the heartbeat's own gate)

        while not stop_event.is_set():
            try:
                if self.cfg.wait_channel_enabled:
                    started = _time.monotonic()
                    task = self.client.wait_for_interactive(
                        team_id=self.cfg.team_id, env_id=self.cfg.env_id,
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
                        team_id=self.cfg.team_id, env_id=self.cfg.env_id,
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

        interval = self.cfg.poll_interval_seconds
        try:
            while True:
                # While the host is overloaded, wait at the guard's (shorter) re-check cadence rather
                # than burning full poll cycles — so the lane RESUMES promptly when resources recover.
                if not self.resources.wait_until_ok(stop_event=stop_event):
                    return  # stopped while paused
                try:
                    self.run_once()
                except Exception as e:  # noqa: BLE001 — a transient error must not kill the loop
                    log.error("scan failed: %s", e)
                if stop_event is not None and stop_event.wait(interval):
                    return
                if stop_event is None:
                    time.sleep(interval)
        finally:
            fast_stop.set()
