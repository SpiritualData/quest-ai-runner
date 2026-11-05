"""QAR keeps the Quest wording in the hub's terminal blocks: what users see is the Quest app's sentence, not the hub's generic one."""
from hubofhubs.the_consciousness.terminal import strings

import quest_ai_runner.textual_ui_trail  # noqa: F401  (the terminal's setup imports the Quest wording)
from quest_ai_runner.terminal_words import QUEST_WORDS


def event(visible=False):
    """Four records read (a group only opens behind a summary when more than three were read)."""
    items = [{"ref": t, "label": t, "kind": "quest", **({"visible_to_all": True} if visible else {})}
             for t in ("Run a half marathon", "Morning pages", "Candle shop", "Spanish")]
    return {"v": 1, "step_id": "r", "kind": "records", "state": "done", "counts": {"read": 4}, "affects_user": False, "label_key": "",
            "solution_id": "x", "items": items}


def test_the_terminal_setup_registers_the_quest_words():
    for key, value in QUEST_WORDS.items():
        assert strings.say(key) == value
    assert strings.say("workTrace.group.records") == "From your Quest"
    assert strings.say("workTrace.group.recordsShared") == "From your team's Quest"
    assert strings.say("workTrace.recordKind.quest") == "Quest"


def test_a_rendered_trail_reads_as_quest():
    from hubofhubs.the_consciousness.terminal import trail
    group = next(g for g in trail.expanded_groups(trail.apply_event([], event()), False) if g["id"] == "records")
    assert group["title"] == "From your Quest"
    assert any(row.endswith("Quest") for row in group["rows"])
    shared = trail.expanded_groups(trail.apply_event([], event(visible=True)), True)
    assert next(g for g in shared if g["id"] == "records")["title"] == "From your team's Quest"
