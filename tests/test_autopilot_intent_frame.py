"""The intent frame in the autopilot brief: why the quest exists, computed by the server (no model call),
read once per quest, placed before the goal ladder, absent for a person with no intents, and cut before the
request tier when the brief is over budget."""
from quest_ai_runner.core import prompt_budget
from quest_ai_runner.runner.autopilot import AutopilotPass, compose_batch_text
from quest_ai_runner.runner.autopilot_briefs import INTENT_FRAME_PREAMBLE

from .test_autopilot import FakeAutopilotClient, _now, _quest
from .test_autopilot_goal_ladder import FULL_LADDER_PAYLOAD

FRAME = ("Serves: Health: I am strong.  (card intent-int_1)\nBuilds: Not tied to a solution yet\n"
         "Ideals: Strong body\nGaps: ideal \"Deep sleep\" has no solution")
LADDER = [{"scope": "week", "goals": [{"title": "Run 10k"}]}]


class FrameClient(FakeAutopilotClient):
    """The fake pass client plus the one intents call the brief makes."""

    def __init__(self, *args, frame=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.frame, self.frame_calls = frame, []

    def get_intent_frame(self, quest_id):
        self.frame_calls.append(quest_id)
        return self.frame


def test_the_frame_rides_with_its_use_stated_once_and_sits_before_the_goal_ladder():
    text = compose_batch_text("Run a marathon", None, intent_frame=FRAME, goal_ladder=LADDER)
    assert INTENT_FRAME_PREAMBLE in text and text.count(INTENT_FRAME_PREAMBLE) == 1
    assert FRAME in text
    assert text.index(FRAME) < text.index("THE PERSON'S CURRENT GOALS")
    assert "—" not in INTENT_FRAME_PREAMBLE


def test_no_frame_composes_byte_identically_to_before():
    assert compose_batch_text("Run a marathon", None, intent_frame=None) == compose_batch_text("Run a marathon", None)
    assert compose_batch_text("Run a marathon", None, intent_frame="") == compose_batch_text("Run a marathon", None)


def test_the_frame_is_cut_before_the_request_when_the_brief_is_over_budget(monkeypatch):
    import quest_ai_runner.runner.autopilot_briefs as briefs
    monkeypatch.setattr(briefs, "AUTOPILOT_BRIEF_TOKEN_BUDGET", 60)
    text = compose_batch_text("Run a marathon", None, intent_frame=FRAME * 30)
    assert "Quest outcome: Run a marathon" in text


def test_a_pass_reads_the_frame_once_per_quest_and_puts_it_in_the_task():
    q1 = _quest("q1", mode="act", outcome="Run a marathon")
    client = FrameClient(quests=[q1], goals_by_quest={"q1": FULL_LADDER_PAYLOAD}, frame={"text": FRAME, "anchor": {}, "below": []})
    AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert client.frame_calls == ["q1"] and FRAME in client.created_tasks[0]["text"]


def test_an_empty_frame_a_failed_read_and_a_client_that_cannot_ask_add_nothing():
    for client in (FrameClient(quests=[_quest("q1", mode="act", outcome="Run")], frame={"text": "", "anchor": {}, "below": []}),
                   FrameClient(quests=[_quest("q1", mode="act", outcome="Run")], frame=None),
                   FakeAutopilotClient(quests=[_quest("q1", mode="act", outcome="Run")])):
        AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
        assert INTENT_FRAME_PREAMBLE not in client.created_tasks[0]["text"]
