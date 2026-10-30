"""The Narrator's first beat has its own step identity and can never fail a turn.

A provider swallows a model error or refusal and returns a stand-in apology string while recording
the failure under the step it was called with. The narration beat used to be called with
STEP_REPLY, so a failed (or refused) beat was recorded as a failed reply and a long turn was saved
as failed. These tests pin: own step, no emission on failure (detected via the step outcome, not
the stand-in text), and a prompt that does not ask the model to reveal its thinking.
"""
from hubofhubs.ports.model import STEP_NARRATION, STEP_REPLY
from quest_ai_runner.core.orchestrator import EVENT_PARTIAL, Mode, Narrator, _Emitter

STAND_IN = "Sorry, something went wrong on my side."


def run_first_beat(provider):
    events = []

    class Sink:
        def update(self, ev, mode):
            events.append(ev)

    narrator = Narrator(provider=provider, model="m", emit=_Emitter(Sink(), Mode.LIVE, lambda m: None),
                        enabled=True)
    narrator.begin("create an experiment")
    narrator._first_future.result(timeout=5.0)
    return [e for e in events if e.type == EVENT_PARTIAL and e.data.get("narration")]


class RecordingProvider:
    def __init__(self, fail):
        self.fail = fail
        self.steps = []
        self.step_outcomes = {}

    def answer(self, messages, *, model, step=None, system=None):
        self.steps.append(step)
        self.step_outcomes[step] = "ValueError: refusal" if self.fail else None
        return STAND_IN if self.fail else "I'll start from what the quest is aiming for."


def test_narration_uses_its_own_step_not_reply():
    provider = RecordingProvider(fail=False)
    beats = run_first_beat(provider)
    assert provider.steps == [STEP_NARRATION]
    assert STEP_NARRATION != STEP_REPLY
    assert STEP_REPLY not in provider.step_outcomes
    assert len(beats) == 1


def test_failed_narration_is_recorded_only_under_narration_and_emits_nothing():
    provider = RecordingProvider(fail=True)
    beats = run_first_beat(provider)
    assert beats == [], "a failed narration beat must not emit the provider stand-in"
    assert provider.step_outcomes.get(STEP_NARRATION)
    assert STEP_REPLY not in provider.step_outcomes


def test_narration_prompt_does_not_ask_to_reveal_thinking():
    seen = []

    class Spy:
        def answer(self, messages, *, model, step=None):
            seen.append(" ".join(m["content"] for m in messages))
            return "Checking the quest first."

    run_first_beat(Spy())
    text = seen[0].lower()
    assert "your thinking" not in text and "think out loud" not in text


def seen_by_the_model(message):
    """What the first beat's model call is shown of ``message``."""
    shown = []

    class Provider(RecordingProvider):
        def answer(self, messages, *, model, step=None, system=None):
            shown.append(" ".join(str(m.get("content", m)) if isinstance(m, dict) else str(m) for m in messages))
            return super().answer(messages, model=model, step=step, system=system)

    events = []

    class Sink:
        def update(self, ev, mode):
            events.append(ev)

    narrator = Narrator(provider=Provider(fail=False), model="m", emit=_Emitter(Sink(), Mode.LIVE, lambda m: None),
                        enabled=True)
    narrator.begin(message)
    narrator._first_future.result(timeout=5.0)
    return shown[0]


def test_a_long_message_reaches_the_first_beat_whole_so_it_never_reads_as_cut_off():
    message = ("I spoke to my fellow student and he reminded me of the importance of simplifying. " * 8
               + "Ask me questions as needed.")
    assert len(message) > 300 and message in seen_by_the_model(message)


def test_a_message_too_long_even_for_that_is_cut_with_a_marker():
    shown = seen_by_the_model("word " * 2000)
    assert "[truncated" in shown
