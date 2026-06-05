# Streaming & modes

Every run is one of two **modes**. The brain emits the **same** `ProgressEvent`s in both; a
**`ProgressSink`** (chosen by mode) decides what surfaces. The orchestrator never decides messaging
policy — it only emits events; the sink applies the rule. This keeps the surfacing policy in exactly
one place.

| | **LIVE** (attended) | **BACKGROUND** (sent-off / scheduled) |
|---|---|---|
| Who's watching | a human, in real time | nobody |
| Sink | `StreamSink` — forwards **every** event | `MilestoneSink` — surfaces only `result` / `decision` / `milestone` / `done` |
| Surfaces | status ticks, plan/replan, reads, reply, confirm | result, decision-request, real milestones (planning/reading chatter dropped) |
| Driven by | a chat backend / cockpit, **in-process** | the **poller's** poll-by-due lane |

The surfacing rule lives once, in the sink (`SURFACING_EVENTS = {result, decision, milestone,
done}`), so every consumer inherits identical policy.

## Public API (frozen)

```python
from quest_ai_runner.core import Mode, StreamSink, MilestoneSink, ProgressEvent
# orch = build_orchestrator(cfg)  (or Orchestrator(...))

# --- LIVE, in-process streaming (chat backend / cockpit) ---
sink = StreamSink(forward=lambda ev: websocket.send(ev))   # ev is a dict
result = orch.run(user_message, mode=Mode.LIVE, sink=sink)  # streams as it works; returns the result

# ...or iterate events as a generator (event dicts, then the terminal OrchestratorResult):
for item in orch.run_stream(user_message, mode=Mode.LIVE):
    render(item)            # OrchestratorResult last; dict events before it

# --- BACKGROUND execution (the runner/executor does this for you) ---
sink = MilestoneSink(on_result=..., on_decision=..., on_milestone=..., on_done=...)
result = orch.run(task_text, mode=Mode.BACKGROUND, sink=sink)   # only milestones/result/decision surface
```

A plain `orch.run(msg)` with no mode/sink still works (non-streaming; legacy `status` callback only).

## Live → Background handoff (consumer disconnected)

If a live viewer disconnects mid-run, the run **continues** and its remaining events (including the
final result) are delivered via a background sink instead of the dropped stream — this is the
brain's job:

```python
result = orch.run(
    msg, mode=Mode.LIVE, sink=live_stream,
    background_sink=MilestoneSink(...),
    detach_check=lambda: consumer.disconnected,   # polled during the run
)
```

When `detach_check()` turns True, the brain flips sinks (the v1 mechanism is `core.FanoutSink`) and
finishes via the background path (notify/PATCH), so nothing is lost.

**Background → Live** ("a background result shows up when the user returns") is a consumer/UI
concern: the result is already persisted/reported by the background path, so the UI just reads it
back. Core does nothing extra here.

## Why this matters

`runner/executor.py` already runs BACKGROUND with a `MilestoneSink` whose `on_milestone` posts an
optional progress note and whose result/decision flow through the Quest PATCH path. So a task run by
the poller and a message streamed live report **consistently** — same events, same surfacing rule,
different sink.
