"""The binding of the hub's Terminal Experience (``terminal/hub_terminal.py``) and the same 23 requirements scored on the hub's arm.

Free and offline: the hub's scripted sessions (a scripted loop and a fake of the stream runner, no model) are run on the hub's Textual arm through its public names, and each
requirement X1 to X23 is met when every scripted case that carries it passes. ``test_terminal_requirements.py`` scores this package's own conversation screen on the same ids; the
two tables decide the ``selected`` implementation of the solution ``terminal-session`` (see the solution's record). Run with ``-s`` for the table.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from hubofhubs.api import terminal_arm

HUB = Path(__import__("hubofhubs").__file__).resolve().parents[1]
CASES = HUB / "evaluation" / "terminal_session" / "cases.json"
IDS = [f"X{n}" for n in range(1, 24)]


def hub_scenarios():
    sys.path.insert(0, str(HUB / "evaluation" / "terminal_session"))
    sys.path.insert(0, str(HUB))
    import sessions
    return sessions


@pytest.mark.skipif(not CASES.is_file(), reason="the hub's evaluation cases are in the sibling checkout only")
def test_the_hub_arm_against_the_same_23_requirements(capsys):
    pytest.importorskip("textual")
    arm, name, _why = terminal_arm("textual_session", {})
    sessions = hub_scenarios()
    cases = [c for c in json.loads(CASES.read_text()) if c["kind"] == "attend"]
    met = {i: [] for i in IDS}
    for case in cases:
        failed = sessions.run_scenario(arm, case["args"]["scenario"], case["args"]["kit"])
        for i in case.get("requirements", []):
            if i in met:
                met[i].append(not failed)
    ok = {i for i, rows in met.items() if rows and all(rows)}
    with capsys.disabled():
        print(f"\nHub arm {name}: requirements met by scripted sessions: {len(ok)} of {len(IDS)}")
        print("  not covered or not met: " + ", ".join(i for i in IDS if i not in ok))
    assert len(ok) >= 17, "the hub arm must meet at least what this package's own screen meets (17 of 23)"


def test_the_binding_builds_the_hub_terminal_with_the_hub_arm_and_this_packages_words(tmp_path):
    from quest_ai_runner.the_consciousness.terminal.hub_terminal import HubTerminal
    screen = HubTerminal().build(folder=str(tmp_path), person="Assistant", environ={"PATH": "/usr/bin"}, plain=True)
    assert screen.person == "Assistant" and screen.command == "quest-ai-runner chat --screen hub"
    assert hasattr(screen.arm, "attend") and screen.names == ("native", "stream", "loop")
    from hubofhubs.api import strings
    import quest_ai_runner.the_consciousness.terminal.terminal_words  # noqa: F401
    assert strings.say("workTrace.group.records") == "From your Quest"
