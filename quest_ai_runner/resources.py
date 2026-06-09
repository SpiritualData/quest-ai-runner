"""Resource guard — pause new work gracefully under system overload, resume when it clears.

A long-running executor shares its host with everything else (the deep-runner subprocesses it
spawns are the heaviest thing it does). When the HOST is overloaded — memory nearly exhausted,
load climbing — the right move is to stop taking on NEW work, not to crash or thrash. Pausing is
lossless by design: an unclaimed task stays queued on the backend and fires on a later scan, so
deferring pickup IS the resume mechanism. In-flight tasks are never killed.

Everything here is generic and OPT-IN. No limit is enforced unless the consumer sets one, either
in code (``RunnerConfig.resource_limits``) or via environment variables (``ResourceLimits.from_env``):

  QAR_MAX_MEMORY_PERCENT      pause when system memory USAGE exceeds this percent (e.g. 90)
  QAR_MIN_FREE_MEMORY_MB      pause when REMAINING available memory drops below this many MB
  QAR_MAX_LOAD_PER_CORE       pause when the 1-min load average PER CPU CORE exceeds this (e.g. 2.0)
  QAR_RESOURCE_RESUME_MARGIN  percent a tripped metric must clear its limit by before resuming
                              (hysteresis; default 10 — prevents pause/resume flapping)
  QAR_RESOURCE_CHECK_INTERVAL seconds between re-checks while paused (default 30)

Sampling uses only the standard library (``/proc/meminfo`` on Linux, ``os.getloadavg`` on Unix),
with an optional ``psutil`` fallback for other platforms when it happens to be installed. A metric
this host can't read simply disables its limit (logged once) — the guard never pauses on a number
it can't see, and never raises into the poll loop.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Tuple

log = logging.getLogger("quest-ai-runner.resources")


@dataclass
class ResourceLimits:
    """The consumer's overload thresholds. ``None`` = that limit is not enforced.

    All-None (the default) means the guard is disabled entirely — existing deployments are
    untouched unless they opt in.
    """

    max_memory_percent: Optional[float] = None   # pause when used memory % >= this
    min_free_memory_mb: Optional[float] = None   # pause when available memory MB <= this
    max_load_per_core: Optional[float] = None    # pause when loadavg(1m)/cpu_count >= this
    resume_margin_percent: float = 10.0          # hysteresis: clear the limit by this % to resume
    check_interval_seconds: float = 30.0         # re-check cadence while paused

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "ResourceLimits":
        """Build limits from ``QAR_*`` environment variables (unset/blank = not enforced)."""
        env = os.environ if env is None else env

        def num(name: str) -> Optional[float]:
            raw = (env.get(name) or "").strip()
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                log.warning("ignoring %s=%r — not a number", name, raw)
                return None

        limits = cls(
            max_memory_percent=num("QAR_MAX_MEMORY_PERCENT"),
            min_free_memory_mb=num("QAR_MIN_FREE_MEMORY_MB"),
            max_load_per_core=num("QAR_MAX_LOAD_PER_CORE"),
        )
        margin = num("QAR_RESOURCE_RESUME_MARGIN")
        if margin is not None:
            limits.resume_margin_percent = max(0.0, margin)
        interval = num("QAR_RESOURCE_CHECK_INTERVAL")
        if interval is not None:
            limits.check_interval_seconds = max(1.0, interval)
        return limits

    def enabled(self) -> bool:
        return any(v is not None for v in (
            self.max_memory_percent, self.min_free_memory_mb, self.max_load_per_core))


@dataclass
class ResourceSnapshot:
    """One sample of the host. ``None`` = that metric is unreadable on this platform."""

    memory_percent: Optional[float] = None   # used memory as a % of total
    free_memory_mb: Optional[float] = None   # available (reclaimable) memory in MB
    load_per_core: Optional[float] = None    # 1-min load average / cpu core count

    def describe(self) -> str:
        parts = []
        if self.memory_percent is not None:
            parts.append(f"memory {self.memory_percent:.0f}% used")
        if self.free_memory_mb is not None:
            parts.append(f"{self.free_memory_mb:.0f}MB free")
        if self.load_per_core is not None:
            parts.append(f"load/core {self.load_per_core:.2f}")
        return ", ".join(parts) or "no readable metrics"


def _memory_from_proc() -> Optional[Tuple[float, float]]:
    """(available_mb, total_mb) from /proc/meminfo, or None where there is no /proc (non-Linux)."""
    try:
        text = Path("/proc/meminfo").read_text()
    except OSError:
        return None
    fields = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(":"):
            fields[parts[0][:-1]] = parts[1]          # values are in kB
    try:
        total_mb = float(fields["MemTotal"]) / 1024.0
        avail_mb = float(fields.get("MemAvailable") or fields["MemFree"]) / 1024.0
    except (KeyError, ValueError):
        return None
    return avail_mb, total_mb


def _memory_from_psutil() -> Optional[Tuple[float, float]]:
    """psutil fallback for platforms without /proc — only if the consumer installed it."""
    try:
        import psutil  # optional; never a hard dependency
    except ImportError:
        return None
    try:
        vm = psutil.virtual_memory()
        return vm.available / (1024.0 * 1024.0), vm.total / (1024.0 * 1024.0)
    except Exception:  # noqa: BLE001 — sampling must never raise into the poll loop
        return None


def sample_resources() -> ResourceSnapshot:
    """Sample the host with stdlib (+ optional psutil). Unreadable metrics come back None."""
    snap = ResourceSnapshot()
    mem = _memory_from_proc() or _memory_from_psutil()
    if mem:
        avail_mb, total_mb = mem
        snap.free_memory_mb = avail_mb
        if total_mb > 0:
            snap.memory_percent = 100.0 * (1.0 - avail_mb / total_mb)
    try:
        load1 = os.getloadavg()[0]                    # Unix; absent/raises on some platforms
        snap.load_per_core = load1 / float(os.cpu_count() or 1)
    except (OSError, AttributeError):
        pass
    return snap


class ResourceGuard:
    """Tracks whether the host is overloaded, with hysteresis so it doesn't flap.

    The poller asks ``check()`` before taking on new work: True means "paused — defer pickup".
    Entering overload logs a WARNING with the tripped limits; recovery logs at INFO. While paused,
    a metric must clear its limit by ``resume_margin_percent`` before the guard resumes, so a
    value hovering at the boundary doesn't toggle the lane on and off every scan.
    """

    def __init__(self, limits: Optional[ResourceLimits] = None, *,
                 sampler: Callable[[], ResourceSnapshot] = sample_resources):
        self.limits = limits or ResourceLimits()
        self._sampler = sampler
        self._paused = False
        self._unreadable_warned: set = set()

    @property
    def enabled(self) -> bool:
        return self.limits.enabled()

    @property
    def paused(self) -> bool:
        return self._paused

    def check(self) -> bool:
        """Sample once and update state. True = overloaded; don't pick up new work."""
        if not self.enabled:
            return False
        try:
            snap = self._sampler()
        except Exception as e:  # noqa: BLE001 — a broken sampler must never stop the lane
            log.warning("resource sampling failed (%s) — treating resources as OK", e)
            return False
        reasons = self._tripped(snap)
        if reasons and not self._paused:
            self._paused = True
            log.warning(
                "system overload detected (%s) — pausing new task pickup; queued tasks stay on "
                "the backend and will run once resources recover", "; ".join(reasons))
        elif not reasons and self._paused:
            self._paused = False
            log.info("system resources recovered (%s) — resuming task pickup", snap.describe())
        return self._paused

    def wait_until_ok(self, *, stop_event: Optional[threading.Event] = None) -> bool:
        """Block until resources are OK (True) or ``stop_event`` is set while waiting (False).

        Re-checks every ``check_interval_seconds`` so a paused service resumes promptly rather
        than waiting out a full poll interval. A disabled guard returns True immediately.
        """
        while self.check():
            interval = self.limits.check_interval_seconds
            if stop_event is not None:
                if stop_event.wait(interval):
                    return False
            else:
                time.sleep(interval)
        return True

    # --- internal -------------------------------------------------------------

    def _tripped(self, snap: ResourceSnapshot) -> List[str]:
        """The configured limits this snapshot trips (empty = OK to run).

        While paused, each limit is tightened by the resume margin: a max-type metric must drop
        below ``limit * (1 - margin)`` (and a min-type rise above ``limit * (1 + margin)``)
        before it stops counting as tripped — that's the hysteresis.
        """
        margin = self.limits.resume_margin_percent / 100.0
        relax = (1.0 - margin) if self._paused else 1.0   # max-type limits
        boost = (1.0 + margin) if self._paused else 1.0   # min-type limits
        reasons: List[str] = []
        lim = self.limits
        if lim.max_memory_percent is not None:
            if snap.memory_percent is None:
                self._warn_unreadable("memory percent (QAR_MAX_MEMORY_PERCENT)")
            elif snap.memory_percent >= lim.max_memory_percent * relax:
                reasons.append(
                    f"memory {snap.memory_percent:.0f}% used (limit {lim.max_memory_percent:.0f}%)")
        if lim.min_free_memory_mb is not None:
            if snap.free_memory_mb is None:
                self._warn_unreadable("free memory (QAR_MIN_FREE_MEMORY_MB)")
            elif snap.free_memory_mb <= lim.min_free_memory_mb * boost:
                reasons.append(
                    f"{snap.free_memory_mb:.0f}MB free (minimum {lim.min_free_memory_mb:.0f}MB)")
        if lim.max_load_per_core is not None:
            if snap.load_per_core is None:
                self._warn_unreadable("load average (QAR_MAX_LOAD_PER_CORE)")
            elif snap.load_per_core >= lim.max_load_per_core * relax:
                reasons.append(
                    f"load/core {snap.load_per_core:.2f} (limit {lim.max_load_per_core:.2f})")
        return reasons

    def _warn_unreadable(self, label: str) -> None:
        """Warn ONCE per metric that a configured limit can't be read on this host."""
        if label not in self._unreadable_warned:
            self._unreadable_warned.add(label)
            log.warning("%s is configured but unreadable on this platform — that limit is "
                        "not enforced here", label)
