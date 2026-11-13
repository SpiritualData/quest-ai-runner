"""What the chat is told while a name nothing placed keeps a turn going, and how the turn ends.

Each decision of the hub's Goal Completion becomes one ``stop_decision`` progress event (whether it goes on, why, what is left, what is tried next), the turn that
only the person can finish ends with a question written by code from the run's own record, and it is stopped with ``needs_answer``, which is not a limit of tries.
"""
from quest_ai_runner.the_consciousness.judgment_calls.ask_policy import STOP_NEEDS_ANSWER
from quest_ai_runner.the_intelligence.critical_thinking.names_pass import DEEP, FILES, GLOSSARY, WIDER, announce, ask_line, route_words
from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import turn_outcome
from types import SimpleNamespace


class Sink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def test_a_decision_that_goes_on_says_what_is_left_and_what_comes_next():
    sink = Sink()
    announce(sink, {"decision": "continue", "code": "blocked_untried"}, ["ZXKP"], DEEP)
    event = sink.events[0]
    assert event.type == "stop_decision"
    assert event.data == {"continue": True, "decision": "continue", "reason": "blocked_untried", "left": "what ZXKP stands for", "next": "a deeper read-only search"}


def test_a_decision_that_stops_names_no_next_step():
    sink = Sink()
    announce(sink, {"decision": "ask", "code": "blocked_exhausted"}, ["ZXKP", "QRVN"], "")
    data = sink.events[0].data
    assert data["continue"] is False and data["decision"] == "ask" and "next" not in data
    assert data["left"] == "what ZXKP, QRVN stand for"


def test_announcing_with_no_sink_or_a_failing_sink_never_raises():
    announce(None, {"decision": "ask", "code": "x"}, ["ZXKP"])

    class Broken:
        def emit(self, event):
            raise RuntimeError("sink down")
    announce(Broken(), {"decision": "ask", "code": "x"}, ["ZXKP"])


def test_the_question_names_what_was_open_and_where_it_was_looked_for():
    line = ask_line(["ZXKP"], [GLOSSARY, FILES, WIDER, DEEP])
    assert line.startswith("What does ZXKP stand for here?")
    assert "the glossary, the readable files, nearby folders and a deeper read-only search" in line
    assert route_words([GLOSSARY]) == "the glossary" and route_words([]) == ""


def test_a_turn_that_ended_on_an_unplaced_name_needs_the_persons_answer_and_is_no_limit():
    done = SimpleNamespace(deep_results=[], exit_reason="max_turns", goal_verdict={"met": False, "names_unplaced": ["ZXKP"], "stop_decision": "ask"})
    assert turn_outcome(done) == {"outcome": "stopped", "stop_reason": STOP_NEEDS_ANSWER}
    other = SimpleNamespace(deep_results=[], exit_reason="max_turns", goal_verdict={"met": False})
    assert turn_outcome(other)["stop_reason"] == "attempt_cap"
