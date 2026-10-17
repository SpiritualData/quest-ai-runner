"""Offline tests for web_search_live: the arm SKIPs without opt-in, and the scorer needs a right answer in the
observation, no error and no duplicate url. No network.
Run: .venv/bin/python3 -m pytest evaluation/components/test_web_search_live.py -q -p no:cacheprovider"""
import importlib
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402


def result(text="Ulaanbaatar is the capital", error=None, urls=("https://a.example/1",)):
    return {"text": text, "error": error, "urls": list(urls), "backend": "fake", "backend_calls": 1,
            "est_tokens": 10, "latency_ms": 5.0}


CASE = {"id": "WL-X", "query": "capital of Mongolia", "answers": ["Ulaanbaatar"]}


def test_scorer_passes_a_right_answer_and_fails_a_wrong_one():
    score = importlib.import_module("web_search_live.score")
    assert score.score_case(lambda c: result(), CASE)["ok"] is True
    assert score.score_case(lambda c: result(text="nothing relevant"), CASE)["ok"] is False
    assert score.score_case(lambda c: result(error="429"), CASE)["ok"] is False
    assert score.score_case(lambda c: result(urls=("https://a.example/1",) * 2), CASE)["ok"] is False


def test_live_arm_skips_without_opt_in(monkeypatch):
    monkeypatch.delenv("QAR_EVAL_LIVE_SEARCH", raising=False)
    build = common.load_impl("web_research", "web_search_live")
    with pytest.raises(SystemExit) as stopped:
        build(common.CallMeter())
    assert stopped.value.code == 0


def test_every_case_names_an_answer_phrase():
    cases = common.load_cases("web_search_live")
    assert len(cases) >= 10 and all(c["answers"] for c in cases)
