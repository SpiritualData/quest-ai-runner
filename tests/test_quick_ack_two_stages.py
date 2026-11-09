"""The quick acknowledgement in two stages: the code-written placeholder goes out before any model call, the model ack is requested at the
turn's start and emitted when it returns without delaying the reply, and a failed or late ack is dropped and counted."""
import threading
import time

from hubofhubs.api import ack_stage, is_ack, STAGE_MODEL, STAGE_PLACEHOLDER
from quest_ai_runner.the_intelligence.mind.orchestrator import EVENT_PARTIAL, Mode, Narrator, _Emitter
from quest_ai_runner.the_intelligence.mind.response_writer import orchestrator_turn as turn


class Sink:
    def __init__(self):
        self.events = []

    def update(self, event, mode):
        self.events.append((time.monotonic(), event))

    def acks(self):
        return [e for _, e in self.events if is_ack(e)]


class Provider:
    """A fake model: records when it was called, answers after ``delay``, or raises."""

    def __init__(self, line="I'll check your marathon plan and compare it with last week.", delay=0.0, fail=False, log=None):
        self.line, self.delay, self.fail, self.log = line, delay, fail, log if log is not None else []
        self.release = threading.Event()

    def answer(self, messages, *, model, system=None, **kwargs):
        self.log.append("model")
        if self.delay:
            self.release.wait(self.delay)
        if self.fail:
            raise RuntimeError("provider down")
        return self.line


def make(provider, sink=None, log=None):
    sink = sink or Sink()
    emit = _Emitter(sink, Mode.LIVE, lambda text: None)
    return Narrator(provider=provider, model="fast-model", emit=emit, enabled=True), emit, sink


def test_the_placeholder_goes_out_before_any_model_call():
    log = []
    sink = Sink()
    emit = _Emitter(sink, Mode.LIVE, lambda text: log.append("status"))
    provider = Provider(log=log, delay=0.2)
    narrator = Narrator(provider=provider, model="m", emit=emit, enabled=True)
    emit.ack()                       # the run's first act (orchestrator_run_begin), before the narrator starts
    log.append("after-placeholder")
    narrator.begin("how is my marathon plan going?")
    provider.release.set()
    narrator._first_future.result(timeout=5)
    assert log.index("status") < log.index("after-placeholder") < log.index("model"), log
    acks = sink.acks()
    assert [ack_stage(a) for a in acks] == [STAGE_PLACEHOLDER, STAGE_MODEL]


def test_the_model_ack_is_requested_at_turn_start_and_emitted_when_it_returns_without_blocking_the_reply():
    provider = Provider(delay=1.0)
    narrator, emit, sink = make(provider)
    begun = time.monotonic()
    narrator.begin("how is my marathon plan going?")
    assert time.monotonic() - begun < 0.2, "begin() only starts the call"
    deadline = time.monotonic() + 2
    while not provider.log and time.monotonic() < deadline:
        time.sleep(0.01)
    assert provider.log == ["model"], "the call is requested at turn start, not later"
    # The reply path goes on: flush_first never joins the pending call.
    waited = time.monotonic()
    narrator.flush_first()
    assert time.monotonic() - waited < 0.2 and not sink.acks()
    provider.release.set()                      # the model returns
    narrator._first_future.result(timeout=5)
    [ack] = sink.acks()
    assert ack_stage(ack) == STAGE_MODEL and ack.text.startswith("I'll check your marathon plan")
    assert any(e.type == EVENT_PARTIAL and e.data.get("narration") for _, e in sink.events), "voice consumers still get the partial"
    report = narrator.report()
    assert report["outcome"] == "emitted" and report["seconds"] is not None


def test_an_ack_not_ready_before_the_final_reply_is_dropped_and_counted():
    provider = Provider(delay=5)
    narrator, emit, sink = make(provider)
    before = turn.ACK_OUTCOMES["dropped_late"]
    narrator.begin("how is my marathon plan going?")
    time.sleep(0.05)
    narrator.close()                            # the final reply is ready first
    provider.release.set()                      # the model returns late
    narrator._first_future.result(timeout=5)
    assert not sink.acks(), "a late ack is never shown after the reply"
    assert narrator.report()["outcome"] == "dropped_late"
    assert turn.ACK_OUTCOMES["dropped_late"] == before + 1


def test_a_failed_ack_is_counted_and_logged_never_silent(caplog):
    narrator, emit, sink = make(Provider(fail=True))
    before = turn.ACK_OUTCOMES["failed"]
    with caplog.at_level("WARNING", logger="quest-ai-runner"):
        narrator.begin("how is my marathon plan going?")
        narrator._first_future.result(timeout=5)
    assert not sink.acks()
    assert narrator.report()["outcome"] == "failed" and "provider down" in narrator.report()["error"]
    assert turn.ACK_OUTCOMES["failed"] == before + 1
    assert any("Quick acknowledgement failed" in r.getMessage() for r in caplog.records)


def test_a_provider_refusal_is_a_counted_failure_not_a_line():
    class Refusing(Provider):
        step_outcomes = {"narration": "refusal"}
    narrator, emit, sink = make(Refusing(line="I could not complete this reply"))
    narrator.begin("how is my marathon plan going?")
    narrator._first_future.result(timeout=5)
    assert not sink.acks() and narrator.report()["outcome"] == "failed"


def test_the_ack_is_written_at_the_fast_tier_on_every_live_user_turn():
    import inspect
    from quest_ai_runner.the_intelligence.mind.context_intake import orchestrator_run_begin as begin
    source = inspect.getsource(begin)
    assert 'resolve_tier("fast")' in source and "quick_ack_early" in source and "cheap_information_turns" not in source.split("narrator = Narrator(")[1].split("rs.narrator = narrator")[0]
