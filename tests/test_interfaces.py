"""Every solution declares its interface once, in the ``interface.py`` beside its code, and ``quest_ai_runner.run`` reaches it by id."""
import importlib
import pathlib

import pytest

import quest_ai_runner
from quest_ai_runner.the_self.assembler import hub

PACKAGE = pathlib.Path(quest_ai_runner.__file__).parent
INTERFACES = sorted(".".join(("quest_ai_runner", *path.relative_to(PACKAGE).with_suffix("").parts)) for path in PACKAGE.rglob("interface.py"))


@pytest.mark.parametrize("module", INTERFACES)
def test_an_interface_module_imports_and_runs_nothing(module):
    importlib.import_module(module)


def test_the_assembler_imports_every_interface_module():
    assert set(INTERFACES) <= set(hub.declaring_modules())


def test_autopilot_is_the_hubs_proactivity_and_prioritization():
    """WHEN standing work starts and WHAT matters first are the hub's solutions, reached through QAR's one line; QAR adds only Quest's ordering arm under Prioritization (tests/test_quest_ordering.py)."""
    due = quest_ai_runner.run("proactivity", {"schedules": [{"id": "weekly", "cadence": "daily"}], "now": "2026-07-12T09:00:00Z"}, environ={})
    assert isinstance(due, (list, tuple))
    ranked = quest_ai_runner.run("prioritization", {"person": "", "request": "what is the latest Rust version?"}, environ={})
    assert set(ranked) >= {"ordered", "held"}
    from quest_ai_runner.the_body.intake_sources.interface import ContextFeeding
