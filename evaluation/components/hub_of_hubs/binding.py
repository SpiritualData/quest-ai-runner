"""QAR's seat bindings: which QAR module fills which hub area, and how to prove a fake hub can be added by configuration. The
hub's scorer reads this; it never imports QAR."""
import csv
from pathlib import Path

from ai_hub_of_hubs import Binding

from hub_of_hubs import probes

REPO = Path(__file__).resolve().parents[3]
CONSUMER = "quest-ai-runner"


def seats():
    with open(REPO / "esoi" / "hub_seats.csv", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def binding(names_of=None):
    if names_of is None:
        from quest_ai_runner.core import resolve
        names_of = resolve.names
    return Binding(consumer=CONSUMER, root=REPO, seats=seats(), core_dir=REPO / "quest_ai_runner" / "core",
                   records_dir=REPO / "esoi", names_of=names_of, probes=probes.PROBES)
