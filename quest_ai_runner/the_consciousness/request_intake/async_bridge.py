"""Run coroutines and blocking work from synchronous adapter code on ONE long-lived background loop.

QAR's adapter methods are synchronous and run on worker threads, but a consumer's I/O is often async.
A client that binds its transport to the first event loop that touches it (some gRPC/HTTP async
clients) breaks when a later call comes from a different, possibly closed, loop. The fix every
consumer needs is the same: one daemon loop, created lazily, kept open for the process, with calls
marshalled onto it. This module is that, so a consumer does not copy it.

    value = run_coro_sync(fetch())            # block the calling worker thread until done
    run_detached(blocking_fn, arg, key=1)     # fire-and-forget on the loop's bounded executor
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)

LOOP_THREAD_NAME = "qar-background-loop"
background_loop: Optional[asyncio.AbstractEventLoop] = None
background_lock = threading.Lock()


def get_background_loop() -> asyncio.AbstractEventLoop:
    """The shared persistent background loop, started on first use."""
    global background_loop
    if background_loop is not None and not background_loop.is_closed():
        return background_loop
    with background_lock:
        if background_loop is not None and not background_loop.is_closed():
            return background_loop
        loop = asyncio.new_event_loop()

        def spin() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        threading.Thread(target=spin, name=LOOP_THREAD_NAME, daemon=True).start()
        background_loop = loop
        return loop


def run_coro_sync(coro: Awaitable[T]) -> T:
    """Run a coroutine to completion from SYNC code on the persistent background loop."""
    return asyncio.run_coroutine_threadsafe(coro, get_background_loop()).result()


def run_detached(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a BLOCKING function off the caller's thread without a raw thread per call.

    ``fn`` is handed to the background loop and executed through ``asyncio.to_thread``, so it lands on
    that loop's bounded default executor. Returns the ``concurrent.futures.Future`` for the scheduled
    work (callers may ignore it). Safe for functions that themselves call ``run_coro_sync``: the
    executor thread blocks on the loop, which stays free to service that nested call. Never raises: a
    failure to schedule returns ``None``, and an exception inside ``fn`` is logged, not surfaced.
    """
    async def to_thread_call() -> None:
        def guarded() -> None:
            try:
                fn(*args, **kwargs)
            except Exception:  # noqa: BLE001 - detached work never surfaces to a caller
                log.debug("run_detached: %s failed", getattr(fn, "__name__", fn), exc_info=True)
        await asyncio.to_thread(guarded)

    try:
        return asyncio.run_coroutine_threadsafe(to_thread_call(), get_background_loop())
    except Exception:  # noqa: BLE001
        log.debug("run_detached: could not schedule background work", exc_info=True)
        return None
