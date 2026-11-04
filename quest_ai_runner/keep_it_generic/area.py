"""``keep-it-generic``: every part of QAR stays comparable by its metric, and no vendor is built into the brain.

The area arm ``structure-library`` takes the hub's connection scorer as a typed seat and offers the area's one line, ``audit(terms)``:
the vendor-name branches in QAR's own ``core`` for the terms given (the static scan the hub-of-hubs measurement runs), beside the
share of the hub's connections that need configuration and no code. It also names the arm of each solution that runs, read through the
selector of ``core/resolve.py`` (``select-an-implementation``). The records, the structure check and the measurement harness are the
esoi library's, applied through ``esoi/`` and ``evaluation/components``.
"""
from __future__ import annotations

import typing
from pathlib import Path
from typing import Any, Mapping, Sequence

from esoi import implementation, solution
from hubofhubs import registry, scan
from hubofhubs.seats import Genericity, HubScoring

from ..core import resolve

PACKAGE_DIR = Path(__file__).resolve().parent.parent


@solution("keep-it-generic", kind="event", entry="audit", example=["examplevendor"])
@typing.runtime_checkable
class KeepingGeneric(Genericity, typing.Protocol):
    """The hub's ``keep-it-generic`` interface: the vendor-name branches in QAR's own brain for the terms given, and how much of the hub
    connects by configuration."""


@implementation("keep-it-generic", "structure-library")
class StructureLibrary:
    """QAR's audit of its own brain for consumer wording, built on the library's records and the hub's connection scorer."""
    REQUIRES = ()     # the seat it is handed

    def __init__(self, scorer: HubScoring) -> None:
        self.scorer = scorer

    def running_arm(self, solution_id: str) -> str:
        """The name of the implementation of ``solution_id`` that runs now, as the selector of ``core/resolve.py`` reads it."""
        return resolve.chosen_name(solution_id)

    def audit(self, terms: Sequence[str]) -> Mapping[str, Any]:
        branches = scan.scan_core({"qar": list(terms)}, PACKAGE_DIR / "core", PACKAGE_DIR.parent)["qar"] if terms else []
        rows = [row for row in registry.read("hub_capabilities.csv") if row.get("wired", "yes") == "yes"]
        return {"vendor_branches": branches, "config_share": float(self.scorer.metrics({}, rows)["config_share"])}
