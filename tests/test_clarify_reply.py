"""A cut-off or ambiguous message gets ONE clarifying question written from the message alone (the planner's structured ``clarify`` reply shape): the writer
is handed no context, transcript, reads or persona, so it cannot name a topic it never saw, and a draft that breaks the standard is regenerated with the same
restricted input. A normal answer keeps its context, and a CHOICE among known candidates (``ask_first``) keeps its context too. Offline, scripted."""
from __future__ import annotations

from typing import Any, Dict, List

from quest_ai_runner.the_consciousness.terminal.adapters_sinks import StreamSink

from .test_done_evidence import LOOKUP, Provider, build

CUT_OFF = "Hi, right now I want to talk about my..."
WRITER_INPUT = "SECRET-CONTEXT-TOPIC"


class RecordingProvider(Provider):
    def __init__(self, plans, drafts):
        super().__init__(plans)
        self.drafts, self.writer_inputs, self.message = list(drafts), [], ""

    def answer(self, messages, *, model, system=None, **more):  # noqa: ARG002
        self.writer_inputs.append({"messages": messages, "system": system or "", "layers": more.get("layers")})
        if messages[-1]["content"] != self.message:
            return "Restated goal."      # the goal derivation and other helpers, not the reply writer
        return self.drafts.pop(0) if self.drafts else "Which topic did you mean?"


def run_turn(plan: Dict[str, Any], drafts: List[str], message: str = CUT_OFF):
    provider = RecordingProvider([plan], drafts)
    provider.message = message
    events: List[Dict[str, Any]] = []
    build(provider).run(message, sink=StreamSink(events.append), transcript=WRITER_INPUT, rep_preamble="You are the rep of Joshua. " + WRITER_INPUT)
    provider.writer_inputs = [c for c in provider.writer_inputs if c["messages"][-1]["content"] == message]      # the reply writer's calls, not the goal derivation's
    return provider, events


def flat(call: Dict[str, Any]) -> str:
    return str(call["messages"]) + call["system"] + str(call["layers"])


def test_a_cut_off_message_is_written_from_the_message_alone_with_no_context():
    provider, events = run_turn(dict(LOOKUP, reply_shape="clarify"), ["Your message cut off after \"my\"; what did you want to talk about?"])
    assert len(provider.writer_inputs) == 1
    seen = flat(provider.writer_inputs[0])
    assert WRITER_INPUT not in seen and "Joshua" not in seen and CUT_OFF in seen and "ONE short question" in seen


def test_a_clarify_draft_that_breaks_the_standard_is_regenerated_with_the_same_restricted_input():
    provider, events = run_turn(dict(LOOKUP, reply_shape="clarify"), ["Is it the marathon? Or morning pages? Or the candle shop?", "What did you want to talk about?"])
    assert len(provider.writer_inputs) == 2
    assert all(WRITER_INPUT not in flat(call) for call in provider.writer_inputs)
    assert [e["text"] for e in events if e["type"] == "result"][-1] == "What did you want to talk about?"


def test_a_normal_answer_keeps_its_context():
    provider, events = run_turn(dict(LOOKUP, reply_shape="default"), ["It is Tuesday."], message="what day is the call?")
    assert WRITER_INPUT in flat(provider.writer_inputs[0])


def test_a_choice_between_known_candidates_keeps_its_context_for_the_options():
    provider, events = run_turn(dict(LOOKUP, reply_shape="ask_first", asks_first=True), ["Which one: the app quest or the funding quest?"], message="update the quest")
    assert WRITER_INPUT in flat(provider.writer_inputs[0])
