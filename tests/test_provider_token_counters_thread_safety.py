"""A provider's tokens_in/tokens_out/call_count must be isolated PER CALLING THREAD.

quest-ai-runner's Poller builds ONE Orchestrator (and hence one provider, via
build_orchestrator) and reuses it for every task; max_concurrent_tasks (default 2) runs
several tasks' Orchestrator.run() calls concurrently on a thread pool. Orchestrator.run()
resets provider.tokens_in/out to 0 at the start of "this turn" and reads them at the end to
report what the turn cost -- true only if nothing else concurrently touches the same
counters. Before this fix, tokens_in/out were plain shared int attributes: one task's reset
could zero out a sibling task's in-flight count, and concurrent tasks' calls summed into one
shared total, which is how AI-created tasks were observed reporting 200,000+ tokens for a
single task's modest real usage. These tests simulate exactly the Poller's execution model
(one worker thread per concurrently-running task) and assert each thread's own
reset-accumulate-read sequence is unaffected by the others.
"""
import threading
import time

from quest_ai_runner.adapters.claude_cli_provider import ClaudeCliProvider
from quest_ai_runner.adapters.multi_provider import MultiProvider
from quest_ai_runner.core.adapters import ModelProviderBase, ScopedThreadPoolExecutor


class CountingProvider(ModelProviderBase):
    """A minimal ModelProviderBase that reports a fixed per-call token cost, with an
    optional sleep between the read and the accumulate so concurrent callers actually
    interleave (widens the race window instead of relying on GIL luck)."""

    def __init__(self, tokens_per_call: int = 1000, delay: float = 0.0):
        super().__init__()
        self._tokens_per_call = tokens_per_call
        self._delay = delay

    def plan(self, prompt, *, model, tool_schema, layers=None):
        raise NotImplementedError

    def answer(self, messages, *, model, system=None, layers=None):
        if self._delay:
            time.sleep(self._delay)
        self.tokens_in += self._tokens_per_call
        self.tokens_out += self._tokens_per_call
        return "ok"

    def list_models(self):
        return ["claude-sonnet"]


def _run_one_turn(provider, *, calls: int, results: list, index: int) -> None:
    """Reproduce Orchestrator.run()'s own token bookkeeping for ONE turn: reset to 0 at the
    start, make some calls, read the final total at the end -- exactly the pattern at
    core/orchestrator.py's `self.provider.tokens_in = 0` / final EVENT_TOKENS read."""
    provider.tokens_in = 0
    provider.tokens_out = 0
    for _ in range(calls):
        provider.answer([{"role": "user", "content": "hi"}], model="claude-sonnet")
    results[index] = (provider.tokens_in, provider.tokens_out)


def test_concurrent_turns_on_shared_provider_do_not_cross_contaminate():
    """Two 'tasks' (threads) share ONE provider instance, exactly like the Poller's singleton
    Orchestrator/provider under max_concurrent_tasks > 1. Each thread's own reported total
    must reflect only ITS OWN calls, not the other thread's, and must not be reset to 0
    mid-flight by the other thread starting a fresh turn."""
    provider = CountingProvider(tokens_per_call=1000, delay=0.01)
    results = [None, None]
    calls_a, calls_b = 5, 3

    t1 = threading.Thread(target=_run_one_turn, args=(provider,), kwargs={"calls": calls_a, "results": results, "index": 0})
    t2 = threading.Thread(target=_run_one_turn, args=(provider,), kwargs={"calls": calls_b, "results": results, "index": 1})
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    tokens_a, _ = results[0]
    tokens_b, _ = results[1]
    assert tokens_a == calls_a * 1000, f"thread A should see only its own 5 calls, got {tokens_a}"
    assert tokens_b == calls_b * 1000, f"thread B should see only its own 3 calls, got {tokens_b}"


def test_multi_provider_isolates_concurrent_turns_too():
    """The same isolation must hold one layer up, on MultiProvider -- the actual object
    Orchestrator.provider refers to in production (build_orchestrator always wraps the
    configured provider with MultiProvider)."""
    inner = CountingProvider(tokens_per_call=1000, delay=0.01)
    mp = MultiProvider(inner, providers={"anthropic": inner})
    results = [None, None]
    calls_a, calls_b = 6, 2

    def turn(calls, index):
        mp.tokens_in = 0
        mp.tokens_out = 0
        for _ in range(calls):
            mp.answer([{"role": "user", "content": "hi"}], model="claude-sonnet")
        results[index] = (mp.tokens_in, mp.tokens_out)

    t1 = threading.Thread(target=turn, args=(calls_a, 0))
    t2 = threading.Thread(target=turn, args=(calls_b, 1))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    tokens_a, _ = results[0]
    tokens_b, _ = results[1]
    assert tokens_a == calls_a * 1000, f"thread A should see only its own 6 calls, got {tokens_a}"
    assert tokens_b == calls_b * 1000, f"thread B should see only its own 2 calls, got {tokens_b}"


def test_single_threaded_behavior_unchanged():
    """Regression guard: ordinary single-threaded use (the CLI, tests, a lane with
    max_concurrent_tasks=1) must behave exactly as a plain int attribute always did."""
    provider = ClaudeCliProvider()
    assert provider.tokens_in == 0
    assert provider.tokens_out == 0
    provider.tokens_in = 42
    provider.tokens_in += 8
    assert provider.tokens_in == 50
    assert provider.tokens_out == 0


def test_scoped_thread_pool_executor_attributes_worker_usage_to_the_turn():
    """Model calls made by a pool the orchestrator spawns INSIDE a turn (parallel sub-answers,
    the overseer consult, guidance calls) must land in that turn's own count, not vanish into an
    anonymous worker thread's isolated tally.

    A plain ThreadPoolExecutor would make plain thread-local counters under-report: each worker
    thread is a distinct scope by default, so its calls would never be visible to the parent
    thread's reset-then-read sequence. ScopedThreadPoolExecutor fixes that by having every
    worker bill the CREATING thread's scope instead of its own.
    """
    provider = CountingProvider(tokens_per_call=1000)
    provider.tokens_in = 0
    provider.tokens_out = 0

    with ScopedThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(provider.answer,
                                [{"role": "user", "content": "hi"}], model="claude-sonnet")
                   for _ in range(4)]
        for f in futures:
            f.result(timeout=10)

    assert provider.tokens_in == 4 * 1000, (
        f"worker-thread calls must count toward the spawning turn, got {provider.tokens_in}")


def test_scoped_thread_pool_executor_does_not_leak_into_a_concurrent_turn():
    """Two turns running concurrently, each spawning its OWN worker pool, must not cross-count --
    the same isolation the plain thread-local tests above assert, extended to pooled workers."""
    provider = CountingProvider(tokens_per_call=1000, delay=0.01)
    results = [None, None]

    def turn_with_pool(calls, index):
        provider.tokens_in = 0
        provider.tokens_out = 0
        with ScopedThreadPoolExecutor(max_workers=calls) as pool:
            futures = [pool.submit(provider.answer,
                                    [{"role": "user", "content": "hi"}], model="claude-sonnet")
                       for _ in range(calls)]
            for f in futures:
                f.result(timeout=10)
        results[index] = provider.tokens_in

    t1 = threading.Thread(target=turn_with_pool, args=(4, 0))
    t2 = threading.Thread(target=turn_with_pool, args=(3, 1))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert results[0] == 4 * 1000, f"turn A should see only its own 4 pooled calls, got {results[0]}"
    assert results[1] == 3 * 1000, f"turn B should see only its own 3 pooled calls, got {results[1]}"


def test_thread_local_counter_defaults_per_thread_without_explicit_init():
    """A thread that never wrote to the counter sees the class default (0), not another
    thread's value -- covers a fresh worker thread's very first read before any reset."""
    provider = ClaudeCliProvider()
    provider.tokens_in = 999  # written on the main (test) thread

    seen = {}

    def read_from_new_thread():
        seen["value"] = provider.tokens_in

    t = threading.Thread(target=read_from_new_thread)
    t.start()
    t.join(timeout=10)

    assert seen["value"] == 0
    assert provider.tokens_in == 999  # main thread's own value is untouched
