"""CURRENT: the production sinks (``core/adapters_sinks.py``): ``StreamSink`` for LIVE, ``MilestoneSink``
for BACKGROUND (every callback wired, as the runner's executor does) and ``FanoutSink`` for the
LIVE-to-BACKGROUND handoff. Zero model calls, zero network."""
from quest_ai_runner.core.adapters import FanoutSink, MilestoneSink, Mode, ProgressEvent, StreamSink


def make_sinks(shown, broken):
    def forward(payload):
        if broken:
            raise RuntimeError("stream dropped")
        shown.append(payload["type"])

    def callback(event):
        if broken:
            raise RuntimeError("callback failed")
        shown.append(event.type)
    live = StreamSink(forward)
    background = MilestoneSink(on_context=callback, on_milestone=callback, on_decision=callback, on_result=callback,
                               on_done=callback, on_tokens=callback, on_overseer=callback)
    return live, background


def build(meter):  # noqa: ARG001
    def surface(kind, args):
        shown = []
        live, background = make_sinks(shown, args.get("broken", False))
        events = args["events"]
        if kind == "surface":
            mode = Mode.LIVE if args["mode"] == "live" else Mode.BACKGROUND
            sink = live if mode is Mode.LIVE else background
            for name in events:
                sink.update(ProgressEvent(type=name, text="t"), mode)
        elif kind == "handoff":
            fan = FanoutSink(live, background)
            for i, name in enumerate(events):
                if i == args["detach_after"]:
                    fan.detach()
                fan.update(ProgressEvent(type=name, text="t"), Mode.LIVE)
        else:
            raise ValueError(kind)
        return {"surfaced": shown, "survived": True}
    return surface
