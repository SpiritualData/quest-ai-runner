"""What a person reads carries names, never record ids or machine header lines (the 2026-11-16 subscribers email)."""
import pytest

from quest_ai_runner.world.tools.readable_guard import READABLE_NAMES_RULE, machine_strings, readable_reason

SEEN = [
    "Quest subscribers: SOLUTIONS YOU ITERATE: esr_b451d0cb3cec",
    "Here is the update for rep_3d63298c8f4c and quest_0bd34af0d0fa.",
    "Persona rep-rep-4f2a9c wrote this.",
    "SOLUTIONS YOU ITERATE: the welcome email",
    "- **SOLUTIONS YOU ITERATE:** none",
]
REAL = [
    "Quest subscribers: the welcome email and the onboarding course",
    "Open it at https://app.example.org/quest/quest_0bd34af0d0fa/goal_3d63298c8f4c to review.",
    "[the quest](https://app.example.org/q/quest_0bd34af0d0fa) is ready",
    "We solve what you iterate on: a quest_dashboard and a goal_list (no ids here).",
]


@pytest.mark.parametrize("text", SEEN)
def test_machine_text_is_found(text):
    assert machine_strings(text) and readable_reason(text)


@pytest.mark.parametrize("text", REAL)
def test_names_and_real_links_pass(text):
    assert readable_reason(text) is None


def test_the_reason_names_each_offender_once():
    reason = readable_reason("a esr_b451d0cb3cec b", "again esr_b451d0cb3cec and goal_aabbccdd11")
    assert reason.count("esr_b451d0cb3cec") == 1 and "goal_aabbccdd11" in reason


def test_the_email_tool_refuses_before_sending_and_sends_names():
    from quest_ai_runner.body.hands.quest_operations.tools_quest_standard import send_quest_email_spec
    from quest_ai_runner.world.tools.tools import ToolContext
    from quest_ai_runner.world.tools.tools_status import ToolStatus
    sent = []

    class Client:
        def send_quest_email(self, quest_id, **kw):
            sent.append(kw)
            return {"persona": "Zee"}

    handler = send_quest_email_spec({}, client_factory=Client).handler
    ctx = ToolContext()
    bad = handler({"quest_id": "q", "subject": "Quest subscribers: SOLUTIONS YOU ITERATE: esr_b451d0cb3cec", "body": "Hello"}, ctx)
    assert not bad.ok and bad.status is ToolStatus.REFUSED and "esr_b451d0cb3cec" in bad.text and not sent
    good = handler({"quest_id": "q", "subject": "Quest subscribers: the welcome email", "body": "See https://x.org/esr_b451d0cb3cec"}, ctx)
    assert good.ok and len(sent) == 1


def test_the_note_and_ask_tools_refuse_ids():
    from quest_ai_runner.body.hands.quest_operations.tools_quest_ops import add_quest_note_spec
    from quest_ai_runner.world.tools.tools import ToolContext
    from quest_ai_runner.world.tools.tools_status import ToolStatus
    written = []

    class Client:
        def add_quest_note(self, quest_id, text):
            written.append(text)
            return [{"text": text}]

    handler = add_quest_note_spec({}, client_factory=Client).handler
    refused = handler({"quest_id": "q", "text": "Finished the work on esr_b451d0cb3cec."}, ToolContext())
    assert refused.status is ToolStatus.REFUSED and not written
    assert handler({"quest_id": "q", "text": "Finished the welcome email."}, ToolContext()).ok


def test_every_autopilot_batch_brief_carries_the_rule_once():
    from quest_ai_runner.intelligence.proactivity.autopilot import compose_batch_text
    for text in (compose_batch_text("Ship it"), compose_batch_text("SOLUTIONS YOU ITERATE: esr_b451d0cb3cec\nEmail subscribers")):
        assert text.count("NAMES, NEVER IDS.") == 1
