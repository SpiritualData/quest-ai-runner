"""Pacing for work that nobody is waiting on.

A pure-Python loop over tens of thousands of cards keeps the interpreter's lock. Every other thread that makes a blocking
call (a stat, a read) then waits out a whole switch interval each time it wants the lock back. Measured with one busy
thread beside one that made 3,000 stat calls: 8.1 s against 4 ms when idle. ``time.sleep(0)`` every 200 items changed
nothing (7.4 s), because it gives the lock up for no time at all. A real pause of 2 ms after each 30 ms of work cost the busy
thread about 6% and brought the other to 35 ms.

So work whose result nobody is waiting for (a vector seed, a context search the turn has already stopped waiting for)
calls a ``Pacer`` inside its loop. Work someone is waiting for does not: it wants full speed.
"""
from __future__ import annotations

import time

WORK_SECONDS = 0.03    # run this long, then pause
PAUSE_SECONDS = 0.002  # long enough for a waiting thread to get through a burst of blocking calls


class Pacer:
    """Call it inside a loop as often as is convenient (it costs a clock read); it pauses once enough work has run.

    ``start_after`` is a ``time.monotonic()`` value: until then it never pauses. A search the caller is still waiting for
    passes its deadline, so it runs at full speed until the caller stops waiting and then gets out of the way. The default
    paces from the first call; ``Pacer.never()`` does not pace at all."""

    def __init__(self, start_after: float = 0.0, work_seconds: float = WORK_SECONDS,
                 pause_seconds: float = PAUSE_SECONDS):
        self.start_after = start_after
        self.work_seconds = work_seconds
        self.pause_seconds = pause_seconds
        self.last = time.monotonic()

    @classmethod
    def never(cls) -> "Pacer":
        return cls(start_after=float("inf"))

    def __call__(self) -> None:
        now = time.monotonic()
        if now < self.start_after:
            self.last = now
            return
        if now - self.last >= self.work_seconds:
            time.sleep(self.pause_seconds)
            self.last = time.monotonic()
