"""The shared consumer helpers: the background-loop bridge and the signature probes."""
import threading
import time

from quest_ai_runner.core.async_bridge import run_coro_sync, run_detached


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


def test_scaffolding_strip_cuts_the_tail_and_reasoning_markup():
    from quest_ai_runner.core.scaffolding import sanitize_user_text, strip_future_context
    text = "Done.\n=== FUTURE CONTEXT (for similar requests by this user) ===\n- a"
    assert strip_future_context(text) == "Done."
    assert sanitize_user_text("<think>hmm</think>Answer") == "Answer"
    assert sanitize_user_text("```\n<think>x</think>\n```") == "```\n<think>x</think>\n```"
