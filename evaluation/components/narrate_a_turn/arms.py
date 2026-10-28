"""Shared machinery of the arms: the real ``Narrator`` driven for one turn start, and the event sink that reads the first beat."""
import functools
import threading
import time

import common  # noqa: F401  (puts the repository on the path)
from quest_ai_runner.core.orchestrator import EVENT_PARTIAL, Mode, Narrator, _Emitter

MODEL = "claude-haiku"          # the family alias the claude CLI resolves; the tier a first beat runs on (QAR_MODEL_FAST)
BEAT_WAIT_SECONDS = 90.0        # how long the eval waits for a beat that never comes before it records none
LATE_SECONDS = 5.0              # Narrator.flush_first joins the first beat for at most this long, then the turn goes on without it


class BeatSink:
    """The stream sink: keeps the narration events of the turn and wakes the waiter on the first."""

    def __init__(self):
        self.beats = []
        self.first = threading.Event()

    def update(self, event, mode):  # noqa: ARG002
        if event.type == EVENT_PARTIAL and isinstance(event.data, dict) and event.data.get("narration"):
            self.beats.append(event.text.strip())
            self.first.set()


class Finished:
    """Wraps a provider so the eval knows when the beat call has returned (a model that answers with nothing is a quiet turn,
    and the eval must not wait for a beat that will not come). Keeps the answer signature, which the narrator inspects."""

    def __init__(self, provider, done):
        self.provider = provider
        self.answer = self.wrap(provider.answer, done)

    @staticmethod
    def wrap(fn, done):
        @functools.wraps(fn)
        def answer(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            finally:
                done.set()
        return answer

    def __getattr__(self, name):
        return getattr(self.provider, name)


class NoModel:
    """A provider that is never to be called: the floor's narrator has a line and no use for a model."""

    def answer(self, *args, **kwargs):  # noqa: ARG002
        raise AssertionError("the floor asked a model")


def new_narrator(case, provider, model, sink, narrator_class=Narrator):
    return narrator_class(provider=provider, model=model, emit=_Emitter(sink, Mode.LIVE, lambda mode: None), enabled=True,
                    transcript_tail=case.get("recent", ""), prior_narration=case.get("prior_narration"))


def first_beat(case, provider, model, narrator_class=Narrator):
    """The turn start with a model behind the narrator: begin, join as the turn does, then wait for a late beat."""
    sink = BeatSink()
    called = threading.Event()
    narrator = new_narrator(case, Finished(provider, called), model, sink, narrator_class)
    started = time.monotonic()
    narrator.begin(case["message"])
    narrator.flush_first()
    if not called.is_set():
        called.wait(BEAT_WAIT_SECONDS)
    sink.first.wait(1.0)   # the call has returned; the beat is emitted by the same thread an instant later
    seconds = round(time.monotonic() - started, 2)
    return {"beat": sink.beats[0] if sink.beats else None, "seconds": seconds if sink.beats else None}


def fixed_beat(case, line):
    """The turn start with a fixed line relayed through the same narrator (so the repeat detector and the dash cleanup apply)."""
    sink = BeatSink()
    narrator = new_narrator(case, NoModel(), "none", sink)
    started = time.monotonic()
    narrator.relay(line)
    return {"beat": sink.beats[0] if sink.beats else None, "seconds": round(time.monotonic() - started, 3) if sink.beats else None}
