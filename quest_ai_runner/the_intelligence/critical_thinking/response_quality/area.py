"""``system-integrity``: every part of QAR stays comparable by its metric, and no vendor is built into the brain.

The area arm ``structure-library`` is the hub's Integrity arm applied to QAR's own brain: ``audit(terms)`` returns the vendor-name
branches in QAR's own ``core`` for the terms given (the static scan the hub-of-hubs measurement runs), beside how stale the hub's research is and the
share of the hub's connections that need configuration and no code (``config_share``, a metric of Tools and Hands, no longer a seat).
``audit_run(run)`` is the hub's own check of a run record (every step names a status and says why it did not run). The records, the structure
check, the measurement harness and the implementation selector are the esoi library's, applied through ``esoi/`` and ``evaluation/components``;
the entry point to the selector is ``core/resolve.py``.
"""
from __future__ import annotations

import typing
from pathlib import Path
from typing import Any, Mapping, Sequence

from esoi import implementation, solution
from hubofhubs.api import scan
from hubofhubs.api import PromptsAsData, config_share
from hubofhubs.api import Genericity
from hubofhubs.api import Staying

PACKAGE_DIR = Path(__file__).resolve().parents[3]


@solution("system-integrity", kind="event", entry="audit", example=["examplevendor"])
@typing.runtime_checkable
class KeepingGeneric(Genericity, typing.Protocol):
    """The hub's ``system-integrity`` interface: the vendor-name branches in QAR's own brain for the terms given, how stale the hub's
    research is and how much of the hub connects by configuration, and the check of a run record."""


@implementation("system-integrity", "structure-library")
class StructureLibrary:
    """QAR's audit of its own brain for consumer wording, built on the library's scan and the hub's Integrity arm (its run audit, its
    staleness reading and its connection share)."""
    REQUIRES = ()     # the seat it is handed

    def __init__(self, stay: Staying) -> None:
        self.hub = PromptsAsData(stay)

    def audit(self, terms: Sequence[str]) -> Mapping[str, Any]:
        branches = scan.scan_core({"qar": list(terms)}, PACKAGE_DIR / "the_intelligence", PACKAGE_DIR.parent)["qar"] if terms else []
        return {"vendor_branches": branches, "stale_areas": list(self.hub.stay.stale_areas()), "config_share": config_share()}

    def audit_run(self, run: Mapping[str, Any]) -> Sequence[str]:
        return self.hub.audit_run(run)
