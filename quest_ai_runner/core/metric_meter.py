"""Meters: how the runner itself reads the value of a metric, so a run's before and after are not left to the worker's word.

A meter turns a ``SpecSlice`` (the solution a goal serves and its authoritative metric) into one number, or ``None`` when it
cannot. Two meters ship, tried in the order ``CoreSettings.metric_meters`` names (default ``recorded_row`` then
``offline_arm``), and the first number wins:

  ``recorded_row``   the latest row of the repository's results log for the metric's arm, read as the leaderboard reads it.
                     It is cheap, runs nothing, and answers only while the row is current: a row recorded before the
                     measured code changed says nothing about the code now, so a stale row gives ``None``.
  ``offline_arm``    runs the metric's declared command (``esoi run <component> --impl <arm>``) and reads the score. It
                     does so ONLY for an arm that cannot spend money (the metric run policy: offline arms are run on
                     every change, live arms only by decision): its latest row must exist, show no model call (or a
                     simulated one), and the arm must not be listed or declared as paid. The run is also given a budget of
                     zero model calls, a scrubbed environment, its own process group and a time limit, so even a wrong
                     reading of the record cannot spend anything. Any failure, a refusal or a timeout gives ``None``.

A meter never raises and never calls a model. What it returns is what ``esoi leaderboard`` ranks by: the balanced score when
the component records one, else the share of cases passed.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from .goal_runner_contract import kill_process_group
from .runtime_settings import core_settings, process_environment
from .spec_slice import SpecSlice

log = logging.getLogger("quest-ai-runner.metric-meter")

BALANCED_LINE = re.compile(r"balanced score\s+([0-9]*\.?[0-9]+)")
PASSED_LINE = re.compile(r":\s*(\d+)/(\d+) passed")
# A variable that looks like a credential never reaches an arm, whatever the repository's own scrub lists.
SECRETISH = re.compile(r"API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)
# Where a repository's own virtual environment keeps its esoi command, tried before the running interpreter's.
REPO_ESOI_COMMANDS = (".venv/bin/esoi", "venv/bin/esoi")


class MetricMeter(Protocol):
    """Reads one metric's current value."""
    name: str

    def measure(self, spec_slice: SpecSlice, working_dir: Optional[str] = None) -> Optional[float]:
        ...


def repository(spec_slice: SpecSlice) -> Tuple[Any, str, str]:
    """``(esoi config, component, arm)`` of the slice's metric command; the last two are empty when it names no arm."""
    from esoi import config as esoi_config
    from esoi.registry import first_arm
    config = esoi_config.load(Path(spec_slice.root))
    component, arm = first_arm(config, spec_slice.metric_command)
    return config, component, arm


def latest_row(config: Any, component: str, arm: str) -> Optional[Dict[str, str]]:
    from esoi import leaderboard
    return leaderboard.latest_results(config.results_path).get(component, {}).get(arm)


class RecordedRowMeter:
    """The metric's value from the latest row of the results log, while that row is current."""
    name = "recorded_row"

    def measure(self, spec_slice: SpecSlice, working_dir: Optional[str] = None) -> Optional[float]:
        try:
            from esoi import harness, leaderboard
            config, component, arm = repository(spec_slice)
            row = latest_row(config, component, arm) if component else None
            if row is None or harness.stale_reason(config, component, row, strict=True):
                return None
            return float(leaderboard.pass_rate(row))
        except Exception:  # noqa: BLE001  a meter that cannot read says so with None
            log.debug("recorded row not read", exc_info=True)
            return None


class OfflineArmMeter:
    """The metric's value from running its offline arm now."""
    name = "offline_arm"

    def __init__(self, timeout_seconds: Optional[float] = None):
        self.timeout_seconds = timeout_seconds

    def measure(self, spec_slice: SpecSlice, working_dir: Optional[str] = None) -> Optional[float]:
        try:
            from esoi import harness
            config, component, arm = repository(spec_slice)
            row = latest_row(config, component, arm) if component else None
            if row is None or harness.paid_reason(config, component, arm, row):
                return None   # no proof it spends nothing, or proof it may
            argv = (self.esoi_command(config.root) + ["run", component, "--impl", arm, "--max-calls", "0"]
                    + sample_flags(row))
            return self.run(argv, config)
        except Exception:  # noqa: BLE001
            log.debug("offline arm not measured", exc_info=True)
            return None

    @staticmethod
    def esoi_command(root: Path) -> List[str]:
        own = next((str(root / rel) for rel in REPO_ESOI_COMMANDS if os.access(root / rel, os.X_OK)), None)
        return [own] if own else [sys.executable, "-m", "esoi"]

    def run(self, argv: Sequence[str], config: Any) -> Optional[float]:
        limit = self.timeout_seconds or core_settings().metric_arm_timeout_seconds
        proc = subprocess.Popen(list(argv), cwd=str(config.root), env=scrubbed_environment(config), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            kill_process_group(proc)
            proc.communicate()
            log.info("offline arm %s ran longer than %.0fs; no value", argv[-1], limit)
            return None
        return score_from_output(out) if proc.returncode == 0 else None


def sample_flags(row: Dict[str, str]) -> List[str]:
    """The ``esoi run`` flags that score the same cases the recorded row was scored on (none: the full set).

    Read from the row's ``sample`` label alone. ``esoi.harness.sample_args`` also handles rows older than that column, but
    it imports the repository's evaluation modules into this process to do so, which a meter must not."""
    label = (row.get("sample") or "").strip()
    if label.startswith("only:"):
        return ["--only", label[len("only:"):]]
    if label.startswith("sample:"):
        return ["--sample", label[len("sample:"):]]
    return []


def scrubbed_environment(config: Any) -> Dict[str, str]:
    """The environment an arm runs in: the repository's own ``[hermetic]`` scrub, and nothing that looks like a credential."""
    table = config.hermetic if isinstance(config.hermetic, dict) else {}
    prefixes, names = tuple(table.get("prefixes", ())), set(table.get("names", ()))
    env = {k: v for k, v in process_environment().items()
           if not (k.startswith(prefixes) or k in names or SECRETISH.search(k))}
    env.update({str(k): str(v) for k, v in dict(table.get("set", {})).items()})
    return env


def score_from_output(out: str) -> Optional[float]:
    """The score ``esoi run`` printed: the balanced score when it prints one, else cases passed over cases."""
    found = BALANCED_LINE.search(out or "")
    if found:
        return float(found.group(1))
    found = PASSED_LINE.search(out or "")
    return int(found.group(1)) / max(int(found.group(2)), 1) if found else None


METERS = {"recorded_row": RecordedRowMeter, "offline_arm": OfflineArmMeter}


def meters_from_names(names: str) -> List[MetricMeter]:
    """The meters a comma-separated list names, in order; an unknown name is logged and left out."""
    meters: List[MetricMeter] = []
    for name in (n.strip().lower() for n in (names or "").split(",") if n.strip()):
        if name in METERS:
            meters.append(METERS[name]())
        else:
            log.warning("unknown metric meter %r (known: %s)", name, ", ".join(METERS))
    return meters


def read_value(meters: Sequence[MetricMeter], spec_slice: SpecSlice,
               working_dir: Optional[str] = None) -> Tuple[Optional[float], str]:
    """``(value, meter name)`` from the first meter that has a number, else ``(None, "")``."""
    for meter in meters:
        value = meter.measure(spec_slice, working_dir)
        if value is not None:
            return value, meter.name
    return None, ""
