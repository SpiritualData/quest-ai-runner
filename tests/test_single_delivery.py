"""One piece of autopilot work reaches the person ONCE.

Incident 2026-09-25: a persona brief opening "Produce Joshua's dissertation brief for TODAY and
email it to him" ran each work task twice (the first attempt's result was the brief, the verifier
ruled it unmet because nothing was emailed, the retry sent it by hand), and the hand-sent copy
was mailed on top of the automatic copy. These tests pin the fix: the task text states that
delivery is automatic, and the verifier treats "email it" as met by the result, so no attempt is
pushed into sending it by hand.
"""
from quest_ai_runner.core.orchestrator import VERIFY_GOAL_PROMPT
from quest_ai_runner.runner.executor import AUTOMATIC_DELIVERY_NOTE, TaskExecutor

from .test_runner import MockQuestClient


class EmailSettingClient(MockQuestClient):
    def __init__(self, *, email_enabled):
        super().__init__([])
        self._email_enabled = email_enabled

    def get_quest(self, quest_id, **kw):
        return {"quest_id": quest_id, "autopilot": {"email": {"enabled": self._email_enabled}}}


class CapturingOrchestrator:
    """Records the text it was handed, then stops the run
    (the executor reports it failed, which is irrelevant to what is being checked here)."""

    conversation_store = None

    def __init__(self):
        self.texts = []

    def run(self, text, **kwargs):
        self.texts.append(text)
        raise RuntimeError("stop here")


def _run(email_enabled):
    orch = CapturingOrchestrator()
    ex = TaskExecutor(EmailSettingClient(email_enabled=email_enabled), orch)
    ex.execute({"id": "atask_work1", "goal_id": "quest_abc",
                "text": "Produce today's brief and email it to him."})
    return orch


def test_a_mailing_quest_tells_the_run_its_result_is_the_delivery():
    orch = _run(email_enabled=True)
    assert orch.texts[0].startswith("Produce today's brief and email it to him.")
    assert AUTOMATIC_DELIVERY_NOTE in orch.texts[0]


def test_a_quest_that_does_not_mail_gets_no_delivery_note():
    orch = _run(email_enabled=False)
    assert AUTOMATIC_DELIVERY_NOTE not in orch.texts[0]


def test_the_verifier_counts_email_it_as_met_by_the_result():
    assert "PLATFORM DELIVERY" in VERIFY_GOAL_PROMPT
