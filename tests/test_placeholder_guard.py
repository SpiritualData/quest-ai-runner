"""Placeholder text is never saved to a note or a goal."""
import pytest

from quest_ai_runner.the_world.tools.placeholder_guard import placeholder_reason

SEEN = [
    "Research note (pending): daily qigong minutes needed to notice an energy shift. Findings to be added from the investigation tool result, graded by how sure each finding is.",
    "Research note on daily qigong and calm: findings from the investigation run this turn are attached to the quest's investigations list. Review that investigation for the minutes-per-day figure and its confidence level.",
    "Research check on daily tai chi: a quick review was recorded. Open the investigation for the sources and how sure each finding is. {{findings}}",
    "Findings:\n",
    "Research note\nFindings:\nSources:\n- Short sessions help (fairly sure; example.org)",
    "- ",
    "<insert findings here>",
    "[TBD]",
]
REAL = [
    "Research note: how long daily qigong takes to notice a change in calm.\nHow sure: fairly sure.\n- Short daily sessions of 10 to 20 minutes are the usual advice (fairly sure; nccih.nih.gov).\nNot established: a minimum number of minutes.",
    "Call the plumber on Friday to book the tile delivery.",
]


@pytest.mark.parametrize("text", SEEN)
def test_placeholder_content_is_refused(text):
    assert placeholder_reason(text)


@pytest.mark.parametrize("text", REAL)
def test_real_content_passes(text):
    assert placeholder_reason(text) is None


def test_the_note_tool_refuses_placeholder_text_and_writes_real_text():
    from quest_ai_runner.the_body.hands.quest_operations.tools_quest_ops import add_quest_note_spec
    from quest_ai_runner.the_world.tools.tools import ToolContext
    from quest_ai_runner.the_world.tools.tools_status import ToolStatus
    written = []

    class Client:
        def add_quest_note(self, quest_id, text):
            written.append(text)
            return {"id": "n1"}
    tool = add_quest_note_spec({}, lambda: Client())
    refused = tool.handler({"quest_id": "q1", "text": SEEN[0]}, ToolContext(quest_id="q1"))
    assert not refused.ok and refused.status is ToolStatus.REFUSED and written == []
    assert tool.handler({"quest_id": "q1", "text": REAL[0]}, ToolContext(quest_id="q1")).ok and written == [REAL[0]]
