"""A task's tool-call ledger: what each tool call answered, kept by the runner and never by a model.

The goal loop used to learn that a tool the work needs was broken only from the verifier's label
(``tool_failure``). When the verifier called the same broken tool ``more_work`` instead, the loop
kept re-running the worker, one stronger model at a time, against a tool that could not answer.

Every tool call goes through ``ToolRegistry.invoke``, in the brain's process or in the deep
worker's shell (``python -m quest_ai_runner.tools call``). ``invoke`` appends one line per call to
this ledger, keyed by the task id, so the goal loop can read, for any attempt, which tools failed
and with what, and which calls changed something. The record is structural: it comes from the
tool's own result (``ok`` and the text the tool returned), not from the wording of anything a model
wrote.

A failure is *unrecovered* when the last call of that tool in the window failed. The same
unrecovered failure (same tool, same signature) in two consecutive attempts is a broken tool, not
a worker's mistake: running the worker again is the thing that cannot help.
"""
from __future__ import annotations

import difflib
import logging
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Optional

from .jsonl_ledger import LEDGER_MAX_AGE_SECONDS, KeyedJsonlFiles, default_directory  # noqa: F401  (age re-exported)

log = logging.getLogger("quest-ai-runner.tool_ledger")

# Two attempts "say the same thing" when their verdict reasons overlap this much, and their outputs
# do (the no-progress guard in the goal loop).
STUCK_REASON_ALIKE = 0.85
STUCK_OUTPUT_ALIKE = 0.8

# How much of a failing tool's text names it in a report.
DETAIL_CHARS = 240

_ID_RE = re.compile(r"\b[a-z]+_[0-9a-f]{8,}\b|\b[0-9a-f]{24}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]{12,}\b")
_STATUS_RE = re.compile(r"(?<![\w.])([45]\d\d)(?![\w.])")
_NUMBER_RE = re.compile(r"\d+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s|\n")


@dataclass
class ToolOutcome:
    """One tool call as the runner saw it."""
    tool: str
    ok: bool
    signature: str = ""      # failures only: tool, HTTP-style status if any, normalised first sentence
    detail: str = ""         # failures only: the tool's own text, trimmed, for a plain report
    mutates: bool = False    # the tool acts (writes, sends); a success of it is a real change
    at: float = 0.0


def failure_signature(tool: str, text: str) -> str:
    """Stable identity of a tool failure: the same cause gives the same string on every call.

    Ids and numbers are blanked so a different goal id or timestamp does not make the same outage
    look new; an HTTP-style 4xx/5xx status is kept, because 500 and 403 are different problems.
    """
    flat = " ".join((text or "").split()).lower()
    status = _STATUS_RE.search(flat)
    first = _SENTENCE_END.split(flat, maxsplit=1)[0]
    first = _ID_RE.sub("<id>", first)
    first = _NUMBER_RE.sub("#", first)
    return f"{tool}|{status.group(1) if status else ''}|{first[:100].strip()}"


def outcome_from_result(tool: str, ok: bool, text: str, mutates: bool) -> ToolOutcome:
    flat = " ".join((text or "").split())
    return ToolOutcome(tool=tool, ok=bool(ok), mutates=bool(mutates), at=time.time(),
                       signature="" if ok else failure_signature(tool, flat),
                       detail="" if ok else flat[:DETAIL_CHARS])


class FileToolLedger:
    """An append-only JSON-lines file per task in a shared directory (``jsonl_ledger.KeyedJsonlFiles``).

    A file, because the deep worker calls tools from a separate process: the brain and the worker
    share nothing in memory, but they share the machine's temp directory. One whole-line append
    per call under a lock, so concurrent calls do not interleave.
    """

    def __init__(self, directory: Optional[str] = None):
        self.directory = directory or default_directory("qar-tool-ledger")
        self.files = KeyedJsonlFiles(self.directory)

    def path_for(self, task_id: str) -> str:
        return self.files.path_for(task_id)

    def record(self, task_id: Optional[str], outcome: ToolOutcome) -> None:
        """Append one call. Never raises: a missing ledger must not break a tool call."""
        if task_id and self.files.append(task_id, asdict(outcome)):
            self.prune()

    def mark(self, task_id: Optional[str]) -> int:
        """How many calls the task has recorded so far: the start of the next window."""
        return len(self.since(task_id, 0)) if task_id else 0

    def since(self, task_id: Optional[str], mark: int) -> List[ToolOutcome]:
        """The calls recorded after the first ``mark`` of them."""
        if not task_id:
            return []
        rows: List[ToolOutcome] = []
        for row in self.files.read(task_id):
            try:
                rows.append(ToolOutcome(**row))
            except TypeError:
                continue
        return rows[max(0, mark):]

    def prune(self) -> None:
        self.files.prune()


def unrecovered_failures(window: Iterable[ToolOutcome]) -> Dict[str, ToolOutcome]:
    """Signature -> the failure, for each tool whose LAST call in the window failed."""
    last: Dict[str, ToolOutcome] = {}
    for call in window:
        last[call.tool] = call
    return {c.signature: c for c in last.values() if not c.ok and c.signature}


def repeated_failure(previous: Dict[str, ToolOutcome],
                     current: Dict[str, ToolOutcome]) -> Optional[ToolOutcome]:
    """A failure present, unrecovered, in both consecutive windows (first one found), or None."""
    for signature, failure in current.items():
        if signature in previous:
            return failure
    return None


def changed_something(window: Iterable[ToolOutcome]) -> bool:
    """A tool that acts succeeded in the window: the attempt changed something the ledger can see."""
    return any(c.ok and c.mutates for c in window)


def alike(first: str, second: str, threshold: float) -> bool:
    """Whether two texts say the same thing, by character overlap (for text a person reads).

    Compares two outputs with each other; it never looks for words in them. Both empty is not
    alike: nothing was said twice.
    """
    a = " ".join((first or "").lower().split())[:2000]
    b = " ".join((second or "").lower().split())[:2000]
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


def describe_failure(failure: ToolOutcome) -> str:
    """One plain sentence naming the tool and what it answered, for the task's report."""
    detail = failure.detail.rstrip(". ")
    return (f"The {failure.tool} tool failed the same way on two attempts in a row"
            + (f": {detail}." if detail else "."))
