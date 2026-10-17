"""The shared consumer helpers: the background-loop bridge and the signature probes."""
import dataclasses
import threading
import time

from quest_ai_runner.core.async_bridge import run_coro_sync, run_detached
from quest_ai_runner.core.signature_probe import accepted_fields, accepted_keywords, filter_keywords


def test_run_coro_sync_uses_one_persistent_loop():
    async def which_thread():
        return threading.current_thread().name
    assert run_coro_sync(which_thread()) == run_coro_sync(which_thread())


def test_run_detached_runs_off_thread_and_swallows_errors():
    seen = []
    run_detached(lambda: seen.append(1))
    run_detached(lambda: 1 / 0)
    for _ in range(50):
        if seen:
            break
        time.sleep(0.02)
    assert seen == [1]


def test_signature_probes():
    def fn(a, b=1, *, c=2): ...
    def loose(**kw): ...
    @dataclasses.dataclass
    class Cfg:
        x: int = 0
        y: int = 0
    assert accepted_keywords(fn) == {"a", "b", "c"} and accepted_keywords(loose) is None
    assert accepted_fields(Cfg) == {"x", "y"}
    assert filter_keywords({"a": 1, "z": 2}, {"a"}, "t") == {"a": 1}
    assert filter_keywords({"z": 2}, None, "t") == {"z": 2}
