"""The tool that reads a metric's current value from the source its record names.

Role: a flow step of ESOI Application. A metric or solution record names its source in the record's ``source`` column (a short name); the lane's
configuration (``metric_sources`` in the config file, exported to ``QAR_METRIC_SOURCES``) maps each name to a read-only command. ``read_metric_source(name)``
runs that command and returns what it prints: one JSON object ``{"value": ..., "as_of": "YYYY-MM-DD", "source": "<label>", "detail": {...}}``, where
``as_of`` is the day the value covers and ``source`` is the label a person reads. The value is exactly what the command computed: this module never
estimates, rounds or fills in a value, and a command that fails, times out or prints anything else is reported as a gap in its own words.

The configuration is the lane author's statement that each command only reads. The tool does not mutate, and a dry run (``dry_run_writes``) lets exactly
the configured commands start, because they are declared reads; any other process stays refused.

Configuration shape (TOML, one table per source)::

    [metric_sources.<name>]
    command = ["python3", "-I", "/path/to/reader.py"]    # argv, no shell; {python} and $VAR expand like a custom tool's command
    label = "shown when the command prints no source"    # optional
    timeout_seconds = 90                                 # optional, default 60
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from quest_ai_runner.the_world.tools.tools import (
    STANDARD_TOOL_BUILDERS, ToolContext, ToolResult, ToolSpec, expand_placeholders, process_environment)
from quest_ai_runner.the_world.tools.tools_status import ToolStatus

log = logging.getLogger(__name__)

ENV_VAR = "QAR_METRIC_SOURCES"
DEFAULT_TIMEOUT_SECONDS = 60.0
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


class MetricSourceError(RuntimeError):
    """The source could not be read; the message says why in plain words."""


def configured_sources(env: Optional[Mapping[str, str]] = None) -> Dict[str, Dict[str, Any]]:
    """``{name: {command, label, timeout_seconds}}`` from ``QAR_METRIC_SOURCES`` (JSON). Empty when none is configured; a malformed entry is dropped and logged."""
    env = process_environment() if env is None else env
    raw = str(env.get(ENV_VAR) or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as error:
        log.warning("%s is not valid JSON, so no metric source is offered: %s", ENV_VAR, error)
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for name, entry in (data.items() if isinstance(data, dict) else []):
        command = entry.get("command") if isinstance(entry, dict) else None
        if not (isinstance(command, list) and command and all(isinstance(c, str) and c for c in command)):
            log.warning("metric source %r has no command (a non-empty list of strings), so it is not offered", name)
            continue
        out[str(name)] = {"command": command, "label": str(entry.get("label") or ""), "timeout_seconds": float(entry.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)}
    return out


def printed_object(text: str) -> Mapping[str, Any]:
    """The JSON object a command printed: the whole output, else its last non-empty line."""
    text = str(text or "").strip()
    candidates = [text] + ([line for line in text.splitlines() if line.strip()][-1:] if text else [])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise MetricSourceError("the command printed no JSON object")


def reading_of(printed: Mapping[str, Any], label: str = "") -> Dict[str, Any]:
    """The checked reading: ``value`` (as printed), ``as_of`` (a day), ``source`` (a label) and ``detail``. A missing part is an error, never a default."""
    value = printed.get("value")
    if value is None or (isinstance(value, str) and not value.strip()) or isinstance(value, (dict, list)):
        raise MetricSourceError("the command printed no value (a number or a short text)")
    as_of = str(printed.get("as_of") or "").strip()
    if not DAY.fullmatch(as_of):
        raise MetricSourceError(f"the command printed no usable as_of day (got {as_of!r}; it must be YYYY-MM-DD)")
    source = str(printed.get("source") or label or "").strip()
    if not source:
        raise MetricSourceError("the command printed no source label")
    detail = printed.get("detail")
    return {"value": value, "as_of": as_of, "source": source, "detail": detail if isinstance(detail, dict) else {}}


def shown(value: Any) -> str:
    """A value as a person reads it: a whole number without a decimal point, a text as it is."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def read_source(name: str, env: Optional[Mapping[str, str]] = None, run=subprocess.run) -> Dict[str, Any]:
    """Run the command mapped to ``name`` and return its checked reading. Raises ``MetricSourceError`` for an unknown name, a failed or slow command, or an unreadable print."""
    sources = configured_sources(env)
    entry = sources.get(str(name or "").strip())
    if entry is None:
        known = ", ".join(sorted(sources)) or "none"
        raise MetricSourceError(f"no metric source is configured under the name {str(name)!r} (configured: {known})")
    argv = [expand_placeholders(c) for c in entry["command"]]
    try:
        from quest_ai_runner.the_world.tools import dry_run_writes
        if dry_run_writes.active():
            dry_run_writes.declare_read_command(argv)
    except Exception:  # noqa: BLE001 -- the test harness's bookkeeping never stops a read
        pass
    try:
        proc = run(argv, capture_output=True, text=True, timeout=entry["timeout_seconds"], stdin=subprocess.DEVNULL, env=dict(process_environment()))
    except subprocess.TimeoutExpired as error:
        raise MetricSourceError(f"the {name} source did not answer within {entry['timeout_seconds']:g} seconds") from error
    except Exception as error:  # noqa: BLE001 -- a command that cannot start is a gap with its cause
        raise MetricSourceError(f"the {name} source could not be started ({type(error).__name__}: {error})") from error
    if proc.returncode != 0:
        said = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise MetricSourceError(f"the {name} source failed: {said[-400:]}")
    return reading_of(printed_object(proc.stdout), entry["label"])


def checked_reading(text: str, label: str = "") -> Dict[str, Any]:
    """The checked reading of a source that printed ``text`` (a JSON object, whole or on its last line): ``value``, ``as_of``, ``source`` and ``detail``. Raises ``MetricSourceError``
    in plain words when the text is no object or lacks a value, a day or a source label. A host that reads a source some other way than a command (a file on the team's file
    server) gives the text it read to this, so every source is held to the same shape."""
    return reading_of(printed_object(text), label)


def metric_reader_spec(read: Callable[[str], Mapping[str, Any]], names: Optional[Sequence[str]] = None, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> ToolSpec:
    """The ``read_metric_source`` tool over ``read(name) -> reading`` (which raises ``MetricSourceError`` for a source it cannot read). ``names`` are the sources the host offers,
    when it knows them up front; with none the name is free text the reader resolves."""
    offered = sorted(names or [])

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name") or "").strip()
        try:
            reading = read(name)
        except MetricSourceError as error:
            return ToolResult(ok=False, status=ToolStatus.FAILED, text=f"Not read: {error}. Record no value for it.")
        return ToolResult(ok=True, data={"name": name, **reading},
                          text=f"Metric source {name}: {shown(reading['value'])} (as of {reading['as_of']}, from {reading['source']}).")

    name_schema: Dict[str, Any] = {"type": "string", "description": "The source's name, as the metric or solution record's source column gives it."}
    if offered:
        name_schema["enum"] = offered
    return ToolSpec(
        name="read_metric_source",
        handler=handler,
        description=("Read a metric's current value from the source its record names: reads that source (read only) and returns exactly what it "
                     "computed, the value, the day it covers (as_of) and the source label, with the figures behind it in detail. "
                     + (("Configured sources: " + ", ".join(offered) + ".") if offered else "")).strip(),
        when_to_use=("A metric record (or its solution) names a source and you need the metric's real current value: to state it, or to record it. "
                     "Use the value, date and source label exactly as returned."),
        when_not_to_use=("A metric whose record names no source (read the data it points to instead). Estimating, rounding or adjusting the value. "
                         "Recording it: this only reads."),
        parameters={"type": "object", "required": ["name"], "properties": {"name": name_schema}},
        mutates=False, workflow_step=False, origin="standard",
        keywords=("metric", "value", "current", "source", "measure", "number", "compute", "funnel", "pass", "rate", "count", "milestone"),
        timeout_seconds=float(timeout_seconds) + 15.0)


def read_metric_source_spec(env: Mapping[str, str]) -> Optional[ToolSpec]:
    names = sorted(configured_sources(env))
    if not names:
        return None
    return metric_reader_spec(lambda name: read_source(name, env), names, max(c["timeout_seconds"] for c in configured_sources(env).values()))


STANDARD_TOOL_BUILDERS["read_metric_source"] = read_metric_source_spec
