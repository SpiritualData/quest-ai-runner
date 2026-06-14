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

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import RunnerConfig, build_orchestrator, derive_capabilities
from ..resources import ResourceGuard, ResourceLimits
from .executor import TaskExecutor
from .quest_client import QuestApiError, QuestClient, QuestDecisionSink, QuestNotConfigured

log = logging.getLogger("quest-ai-runner.poller")


def _task_signature(task: Dict[str, Any]) -> str:
    """A stable per-task signature so each task fires exactly once.

    Includes status + an updated/scheduled marker so a re-queued or rescheduled task is treated
    as new, but the same queued task seen twice is deduped.
    """
    tid = task.get("id") or task.get("task_id") or ""
    marker = task.get("updated_at") or task.get("scheduled_time") or task.get("scheduled_date") or ""
    return f"{tid}:{task.get('status', 'queued')}:{marker}"


class StateStore:
    """JSON-backed signature store (watchdog_state.json generalized; pluggable backend)."""

    def __init__(self, path: Optional[str]):
        self._path = Path(path) if path else None
        self._handled: set = set()
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if self._path and self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._handled = set(data.get("handled", []))
            except (json.JSONDecodeError, OSError):
                log.warning("state file corrupt/unreadable; starting fresh")

    def _save(self):
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Cap the stored set so it can't grow unbounded over a long-running service.
            recent = list(self._handled)[-5000:]
            self._path.write_text(json.dumps({"handled": recent}, indent=2))
        except OSError as e:
            log.warning("could not persist state: %s", e)

    def seen(self, sig: str) -> bool:
        with self._lock:
            return sig in self._handled

    def mark(self, sig: str):
        with self._lock:
            self._handled.add(sig)
            self._save()


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
        self.client = client or QuestClient(
            config.quest_base_url, config.quest_api_key, team_id=config.team_id)
        # Default the escalation sink to a Quest decision-request sink if the consumer didn't set one.
        if config.escalation is None and self.client.configured:
            config.escalation = QuestDecisionSink(
                self.client, default_assignee_user_id=config.default_assignee_user_id)
        self.state = StateStore(state_path)
        self._orchestrator = None  # built lazily so an unconfigured poll degrades cleanly
        # Capabilities this runner can HONESTLY report, derived from the wired adapters
        # (corpus=FilesAdapter/corpus, code=deep-runner, web=deep-runner can browse via Claude
        # Code's WebSearch/WebFetch). Computed once at construction (the adapter wiring is fixed
        # for the poller's lifetime).
        self._capabilities = derive_capabilities(config)

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
        # Resource gate AFTER the heartbeat (the backend should still see the env as live) but
        # BEFORE discovery/claiming: an overloaded host takes on NO new work this scan. Skipping
        # is lossless — unclaimed tasks stay queued and fire on a later scan once resources
        # recover (in_progress work is never touched).
        if self.resources.check():
            log.info("host overloaded — skipping task pickup this scan; queued tasks will run "
                     "once resources recover")
            return []
        try:
            # Pass the lane's team_id so discovery is ISOLATED per team: two teams under the same
            # owner share one owner-scoped queue, and an unscoped poll would pull BOTH teams' tasks.
            # team_id="" (a teamless/personal lane) keeps owner-scoped discovery — the prior contract.
            due = self.client.discover_due(
                now=datetime.now(timezone.utc), team_id=self.cfg.team_id or "")
        except (QuestApiError, QuestNotConfigured) as e:
            log.info("discovery unavailable (%s) — will retry next scan", e)
            return []

        fresh = [t for t in due if not self.state.seen(_task_signature(t))]
        if not fresh:
            return []
        log.info("%d due task(s), %d new to handle", len(due), len(fresh))

        handled: List[str] = []
        workers = max(1, min(self.cfg.max_concurrent_tasks, len(fresh)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._handle_one, t): t for t in fresh}
            for fut in futures:
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

    def _handle_one(self, task: Dict[str, Any]) -> Optional[str]:
        sig = _task_signature(task)
        task_id = str(task.get("id") or task.get("task_id") or "")
        # Re-check resources PER TASK: overload can begin mid-scan (earlier tasks in this very
        # batch may be what pushed the host over). Defer BEFORE marking/claiming, so the task is
        # re-discovered and runs on a later scan once resources recover.
        if self.resources.check():
            log.info("host overloaded — deferring task %s to a later scan", task_id)
            return None
        # Resolve WHO will run this (the AI representation/skill) so we can stamp it on the claim:
        # the rep slug if a rep_sync_resolver maps this task to a skill dir, else the runner label.
        target = self._resolve_rep_target(task)
        handler = self._handler_label(target)
        # Mark BEFORE running so a crash mid-run doesn't cause a re-fire loop (backend claim also guards).
        self.state.mark(sig)
        try:
            self.client.claim(task_id, handler=handler)
        except QuestApiError as e:
            # Already claimed by another worker, or transient — skip; the backend is the source of truth.
            log.info("could not claim task %s (%s) — skipping", task_id, e)
            return None
        # Opt-in: refresh this rep's skill file from its Quest profile right before running, so the
        # spawned agent reflects the latest persona + learned corrections. Best-effort: a sync
        # failure is logged and the task still runs (it just uses the last-synced skill file).
        # The pre-run pull (when the direction calls for it) also yields the rep's per-run preamble,
        # so the deep run executes AS that rep with no extra consumer glue.
        rep_preamble = self._pull_rep_for(task, target)
        executor = TaskExecutor(self.client, self._orch())
        outcome = executor.execute(task, rep_preamble=rep_preamble)
        log.info("task %s -> %s", task_id, outcome.status)
        # Opt-in push-back: after the run, write the local skill file back up to Quest when the
        # configured direction asks for it. Best-effort and AFTER the task is reported — a sync
        # failure here must never fail the task.
        self._push_rep_for(task, target)
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
        basename of ``skill_dir`` (the rep slug, e.g. "joshua"/"subham"). Otherwise fall back to
        the runner's configured ``runner_label`` (or None when neither is available)."""
        if target:
            _user_id, skill_dir = target
            slug = Path(str(skill_dir)).name
            if slug:
                return slug
        return self.cfg.runner_label or None

    def _pull_rep_for(self, task: Dict[str, Any], target: Optional[tuple] = None) -> Optional[str]:
        """Best-effort PRE-run pull, gated on direction; returns the rep's per-run preamble or None.

        Only fires when the consumer wired a ``rep_sync_resolver`` (OFF by default) AND the
        configured ``rep_sync_direction`` includes a pull ("pull" or "both"). ``target`` is the
        already-resolved ``(user_id, skill_dir)`` (or None to skip). We ``pull_rep_to_skill`` so
        the local skill file reflects the current persona/corrections, then read its MANAGED
        sections and compose them with the runner's context doctrine into a per-run preamble the
        executor injects into the deep run — so the task runs AS that rep by default, no extra
        consumer glue. Never raises: a sync failure is logged and the run proceeds (with the
        previously synced file, and no preamble from this pull)."""
        if not target:
            return None
        if self.cfg.rep_sync_direction not in ("pull", "both"):
            return None  # direction is push-only: do not pull before the run
        user_id, skill_dir = target
        team_id = task.get("team_id") or self.cfg.team_id
        try:
            from pathlib import Path as _Path

            from ..core.context_doctrine import compose_deep_preamble
            from .rep_sync import SKILL_FILE_NAME, parse_skill_file, pull_rep_to_skill
            pull_rep_to_skill(self.client, team_id, user_id, skill_dir)
            # Read the rep's persona + learned corrections back out of the just-pulled file and
            # build the per-run preamble (doctrine + this rep's managed sections). The skill file's
            # MANAGED sections are the source of the rep's identity for THIS run.
            skill_text = (_Path(skill_dir) / SKILL_FILE_NAME).read_text(encoding="utf-8")
            return self._build_rep_preamble(skill_text, compose_deep_preamble, parse_skill_file)
        except Exception as e:  # noqa: BLE001 — best-effort, like progress posting/heartbeat
            log.info("rep pull for %s failed (%s) — running with existing skill file", user_id, e)
            return None

    @staticmethod
    def _build_rep_preamble(skill_text: str, compose_deep_preamble, parse_skill_file) -> Optional[str]:
        """Compose a deep-run preamble from a skill file's MANAGED sections (persona + learned).

        Generic: it only knows ``persona`` + ``learned_notes`` (the rep_sync managed shape) and the
        runner's context doctrine. Returns None when the file carries no rep identity to inject."""
        parsed = parse_skill_file(skill_text or "")
        persona = (parsed.get("persona") or "").strip()
        learned = parsed.get("learned_notes") or []
        if not persona and not learned:
            return None
        parts: List[str] = []
        if persona:
            parts.append("=== ACT AS THIS PERSON (their persona) ===\n" + persona)
        if learned:
            bullets = "\n".join(f"- {str(n.get('text', '')).strip()}"
                                for n in learned if str(n.get("text", "")).strip())
            if bullets:
                parts.append("=== LEARNED CORRECTIONS (apply these) ===\n" + bullets)
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

    # --- run modes -----------------------------------------------------------

    def run_forever(self, *, stop_event: Optional[threading.Event] = None):
        import time
        interval = self.cfg.poll_interval_seconds
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
