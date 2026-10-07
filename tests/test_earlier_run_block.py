from quest_ai_runner.runner.executor import earlier_run_block


def test_first_run_gets_nothing():
    assert earlier_run_block({"text": "x", "progress": [{"kind": "started", "text": "hi"}]}) == ""


def test_rerun_sees_why_it_stopped_and_the_feed():
    task = {"result": "Stopped by the liveness review: idle 8 minutes.",
            "progress": [{"kind": "started", "text": "Started", "at": "2026-10-06T13:31:23"},
                         {"kind": "status", "text": "Searching context", "at": "2026-10-06T13:32:00"}]}
    out = earlier_run_block(task, "sess-1")
    assert "liveness review" in out and "Searching context" in out and "own session" in out


def test_failed_rerun_without_session_still_sees_result():
    task = {"result": "boom", "progress": [{"kind": "started", "text": "Started"}]}
    assert "boom" in earlier_run_block(task)
