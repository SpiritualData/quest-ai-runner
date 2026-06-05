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
                 client: Optional[QuestClient] = None):
        self.cfg = config
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
        the poll proceeds. The team_id and runner_label come from the consumer's RunnerConfig."""
        if not self.cfg.team_id:
            return  # no team to attach the env to — nothing to heartbeat (still a valid poll)
        try:
            self.client.post_environment_heartbeat(
                self._capabilities,
                runner_label=self.cfg.runner_label,
                team_id=self.cfg.team_id,
            )
        except Exception as e:  # noqa: BLE001 — heartbeat is best-effort, never breaks the scan
            log.info("environment heartbeat failed (%s) — continuing poll", e)

    def _handle_one(self, task: Dict[str, Any]) -> Optional[str]:
        sig = _task_signature(task)
        task_id = str(task.get("id") or task.get("task_id") or "")
        # Mark BEFORE running so a crash mid-run doesn't cause a re-fire loop (backend claim also guards).
        self.state.mark(sig)
        try:
            self.client.claim(task_id)
        except QuestApiError as e:
            # Already claimed by another worker, or transient — skip; the backend is the source of truth.
            log.info("could not claim task %s (%s) — skipping", task_id, e)
            return None
        executor = TaskExecutor(self.client, self._orch())
        outcome = executor.execute(task)
        log.info("task %s -> %s", task_id, outcome.status)
        return task_id

    # --- run modes -----------------------------------------------------------

    def run_forever(self, *, stop_event: Optional[threading.Event] = None):
        import time
        interval = self.cfg.poll_interval_seconds
        while True:
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001 — a transient error must not kill the loop
                log.error("scan failed: %s", e)
            if stop_event is not None and stop_event.wait(interval):
                return
            if stop_event is None:
                time.sleep(interval)
