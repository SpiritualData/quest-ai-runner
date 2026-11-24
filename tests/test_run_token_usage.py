"""A finished task run carries its token usage in ``result_data['usage']``, split into the Claude Code work and the Quest AI Runner's own model calls.

Pinned offline with fakes: the Claude Code arm is counted from the stream-json lines the run relays (each message id once, helpers included), the runner's own
calls come from the turn result, and the total adds every kind as reported (cache reads are not discounted)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from hubofhubs.api import DeepResult, ProgressEvent
from quest_ai_runner.intelligence.mind.delegation.run_facts import RunFacts, end_report_data, usage_facts

KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def line(msg_id, inp, write, read, out, parent=None):
    raw = {"type": "assistant", "message": {"id": msg_id, "usage": {"input_tokens": inp, "cache_creation_input_tokens": write,
                                                                      "cache_read_input_tokens": read, "output_tokens": out}}}
    if parent:
        raw["parent_tool_use_id"] = parent
    return ProgressEvent(type="stream", data=raw)


def run_facts():
    sent = []
    return RunFacts(sent.append), sent


def test_each_streamed_message_counts_once_and_helpers_count_too():
    facts, _ = run_facts()
    for event in (line("m1", 5, 100, 1000, 20), line("m1", 5, 100, 1000, 20), line("m2", 1, 0, 1100, 40), line("h1", 2, 10, 300, 7, parent="toolu_x")):
        facts.take(event)
    assert facts.claude_usage() == {"input": 8, "cache_write": 110, "cache_read": 2400, "output": 67, "calls": 3}


def test_a_message_whose_output_grows_across_lines_counts_its_largest_figures_once():
    facts, _ = run_facts()
    facts.take(line("m1", 3, 50, 900, 1))
    facts.take(line("m1", 3, 50, 900, 120))
    assert facts.claude_usage() == {"input": 3, "cache_write": 50, "cache_read": 900, "output": 120, "calls": 1}


def test_a_run_with_no_stream_reports_no_claude_code_usage():
    facts, _ = run_facts()
    facts.take(ProgressEvent(type="tokens", data={"cost_usd": 0.1}))
    assert facts.claude_usage() is None


def test_the_final_spend_and_the_end_facts_carry_the_usage_split():
    facts, sent = run_facts()
    facts.take(line("m1", 5, 100, 1000, 20))
    result = DeepResult(met=True, output="ok", cost_usd=0.5)
    ended = facts.finish([result])
    expected_cc = {"input": 5, "cache_write": 100, "cache_read": 1000, "output": 20, "calls": 1}
    assert ended["usage"] == {"claude_code": expected_cc, "total_tokens": 1125}
    spends = [e for e in sent if e.type == "spend"]
    assert spends[-1].data["usage"] == ended["usage"]


def test_the_runners_own_calls_join_the_total():
    cc = {"input": 5, "cache_write": 100, "cache_read": 1000, "output": 20, "calls": 1}
    result = SimpleNamespace(deep_results=[SimpleNamespace(claude_code_usage=cc, met=True)], tokens_in=2100, tokens_out=310, llm_calls=4, goal_verdict=None, exit_reason="")
    data = end_report_data(result)
    assert data["usage"] == {"claude_code": cc, "qar_llm": {"input": 2100, "output": 310, "calls": 4}, "total_tokens": 1125 + 2410}


def test_a_turn_with_only_its_own_calls_and_a_turn_with_nothing_known():
    assert usage_facts(SimpleNamespace(tokens_in=7, tokens_out=3, llm_calls=2), []) == {"qar_llm": {"input": 7, "output": 3, "calls": 2}, "total_tokens": 10}
    assert usage_facts(SimpleNamespace(tokens_in=0, tokens_out=0, llm_calls=0), []) is None
    assert end_report_data(SimpleNamespace(deep_results=[], goal_verdict={"met": True})) == {}


def test_the_parser_matches_the_sum_of_unique_message_ids_on_a_real_session():
    folder = os.environ.get("QAR_REAL_SESSION_DIR")      # a folder of real agent session files, kept outside the repository
    sessions = sorted(Path(folder).glob("*.jsonl"), key=lambda p: p.stat().st_size) if folder else []
    def assistants(path):
        return [json.loads(text) for text in path.read_text().splitlines() if '"type":"assistant"' in text.replace(" ", "")]

    sample, raws = None, []
    for path in (p for p in sessions if 300_000 < p.stat().st_size < 3_000_000):
        raws = assistants(path)
        if len({r["message"]["id"] for r in raws if (r.get("message") or {}).get("id")}) > 60:
            sample = path
            break
    if sample is None:
        import pytest
        pytest.skip("no real session log on this machine")
    facts, _ = run_facts()
    unique = {}
    for raw in raws:
        if raw.get("type") == "assistant" and (raw.get("message") or {}).get("usage"):
            facts.take(ProgressEvent(type="stream", data=raw))
            unique[raw["message"]["id"]] = raw["message"]["usage"]
    want = {"input": sum(u.get("input_tokens") or 0 for u in unique.values()), "cache_write": sum(u.get("cache_creation_input_tokens") or 0 for u in unique.values()),
            "cache_read": sum(u.get("cache_read_input_tokens") or 0 for u in unique.values()), "output": sum(u.get("output_tokens") or 0 for u in unique.values()),
            "calls": len(unique)}
    assert want["calls"] > 20 and facts.claude_usage() == want
