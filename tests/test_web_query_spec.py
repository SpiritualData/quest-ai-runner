"""Tests for coerce_web_query -- the robust web-search spec parser.

Covers the varied/nested shapes the orchestrator's planner actually emits (observed in
live runs) that previously passed a nested dict to the search API and failed.
"""
from __future__ import annotations

import pytest

from quest_ai_runner.adapters.web_query_spec import coerce_web_query, coerce_web_query_text

Q = "Portland Marathon 2026 date registration cost"


@pytest.mark.parametrize("spec", [
    {"q": Q},
    {"query": Q},
    {"search": Q},
    {"query": {"q": Q, "max_results": 5}},
    {"query": {"operation": "web_search", "params": {"query": Q}}},
    {"query": {"operation": "web_search", "q": Q}},
    {"query": {"params": {"search": Q}}},
    {"input": {"query": Q}},
])
def test_extracts_query_from_all_planner_shapes(spec):
    assert coerce_web_query_text(spec) == Q


def test_extracts_max_results_from_nested():
    parsed = coerce_web_query({"query": {"q": Q, "max_results": 7}})
    assert parsed["q"] == Q
    assert parsed["max_results"] == 7


def test_extracts_scope_and_topic():
    parsed = coerce_web_query({"q": Q, "scope": "site:portlandmarathon.com", "topic": "registration"})
    assert parsed["scope"] == "site:portlandmarathon.com"
    assert parsed["topic"] == "registration"


def test_operation_token_is_never_used_as_query():
    # Only an operation token, no real query text -> empty (not "web_search").
    assert coerce_web_query_text({"query": {"operation": "web_search"}}) == ""


def test_missing_query_returns_empty():
    assert coerce_web_query_text({"foo": "bar"}) == ""
    assert coerce_web_query_text({}) == ""


def test_never_raises_on_odd_input():
    assert coerce_web_query_text(None) == ""
    assert coerce_web_query_text(42) == ""
    assert coerce_web_query_text([1, 2, 3]) == ""
