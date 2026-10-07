"""The verifier must see THIS TURN'S gathered web evidence, and must trust it over its own
training knowledge (live bug, 2026-10-06): asked "What is the latest stable Python release right
now?", the planner correctly searched the web, the answer was correctly grounded in the results,
but the goal-verification pass (``Orchestrator._verify_goal``) judged it "incorrect" and steered a
regeneration toward a stale, wrong version -- because the verifier never received the gathered web
results in the first place, only the stable L2 ``context_layer`` (cards/corpus), never the volatile
``gathered`` tail the answer call itself used.

Covers:
(a) ``_gathered_has_web_evidence``: true only for a LIVE WEB observation (``rel_path`` starting
    "web_search:" or ``locator`` starting "web extract: "), false for an ordinary read/grep/query
    and for None/empty -- so the fix below never fires for a deployment or a turn with no web.
(b) ``_verify_goal``'s new ``gathered`` parameter: absent/empty is byte-for-byte the old prompt (no
    EVIDENCE section); a non-web gathered item adds an EVIDENCE section but NOT the web-precedence
    note; a web-origin item adds BOTH, placed before the WORKER OUTPUT section, and the note text
    itself (never the evidence content) rides in BOTH the flattened prompt and the layered tail,
    while the cached L1/L2 blocks (persona/standards/context) are UNCHANGED by gathered content
    (``test_verify_context_layer.py``'s L2 byte-identity contract still holds).
(c) ``grounding_answer_tail``'s web-citation carve-out: present only when the gathered content
    includes a web observation, absent for an ordinary corpus read, so the "never a list of
    retrieval hits" voice rule in ``REPLY_VOICE_SYSTEM`` cannot silently swallow a live citation.

All offline: no network, no API key.
"""
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    Orchestrator,
    VERIFY_WEB_EVIDENCE_NOTE,
    _gathered_has_web_evidence,
    grounding_answer_tail,
    grounding_context_layer,
)

from .conftest import StubProvider

from .test_verify_context_layer import LayeredScriptedProvider, cache_blocks_text, make_orch


# --------------------------------------------------------------------------- #
# (a) web-evidence detection
# --------------------------------------------------------------------------- #

def test_no_web_evidence_for_none_or_empty_gathered():
    assert _gathered_has_web_evidence(None) is False
    assert _gathered_has_web_evidence([]) is False


def test_no_web_evidence_for_an_ordinary_read_or_grep():
    gathered = [
        {"kind": "read", "rel_path": "README.md", "locator": "lines 1-10", "text": "hello"},
        {"kind": "grep", "pattern": "foo", "hits": []},
        {"kind": "query", "rel_path": "mongo:quests", "text": "some db text"},
    ]
    assert _gathered_has_web_evidence(gathered) is False


def test_web_evidence_true_for_a_web_search_rel_path():
    gathered = [{"kind": "query", "rel_path": "web_search:python latest release",
                "text": "WEB RESULTS for ...: Python 3.14.0 is the latest stable release."}]
    assert _gathered_has_web_evidence(gathered) is True


def test_web_evidence_true_for_a_web_page_fetch_locator():
    gathered = [{"kind": "read", "rel_path": "https://python.org/downloads",
                "locator": "web extract: https://python.org/downloads",
                "text": "Python 3.14.0 released October 2025."}]
    assert _gathered_has_web_evidence(gathered) is True


def test_web_evidence_ignores_malformed_entries():
    assert _gathered_has_web_evidence(["not a dict", None, 42]) is False


# --------------------------------------------------------------------------- #
# (b) _verify_goal: evidence section + web-precedence note
# --------------------------------------------------------------------------- #

def test_verify_goal_with_no_gathered_is_byte_for_byte_unchanged():
    provider = StubProvider(decisions=[{"met": True, "reason": "done"}])
    orch = make_orch(provider)
    verdict, error = orch._verify_goal("the goal", "the brief", "the output")
    assert verdict is not None and error is None
    assert "EVIDENCE GATHERED THIS TURN" not in provider.last_plan_prompt
    assert "WEB EVIDENCE PRECEDENCE" not in provider.last_plan_prompt


def test_verify_goal_with_empty_gathered_list_is_unchanged():
    provider = StubProvider(decisions=[{"met": True, "reason": "done"}])
    orch = make_orch(provider)
    verdict, error = orch._verify_goal("the goal", "the brief", "the output", gathered=[])
    assert verdict is not None and error is None
    assert "EVIDENCE GATHERED THIS TURN" not in provider.last_plan_prompt


def test_verify_goal_with_non_web_gathered_adds_evidence_but_not_web_note():
    provider = StubProvider(decisions=[{"met": True, "reason": "done"}])
    orch = make_orch(provider)
    gathered = [{"kind": "read", "rel_path": "notes.md", "locator": "notes.md",
                "text": "UNIQUE_CORPUS_FACT_123"}]
    verdict, error = orch._verify_goal("the goal", "the brief", "the output", gathered=gathered)
    assert verdict is not None and error is None
    prompt = provider.last_plan_prompt
    assert "EVIDENCE GATHERED THIS TURN" in prompt
    assert "UNIQUE_CORPUS_FACT_123" in prompt
    assert "WEB EVIDENCE PRECEDENCE" not in prompt


def test_verify_goal_with_web_gathered_adds_evidence_and_web_precedence_note():
    provider = StubProvider(decisions=[{"met": True, "reason": "done"}])
    orch = make_orch(provider)
    gathered = [{"kind": "query", "rel_path": "web_search:python latest release",
                "text": "WEB RESULTS: Python 3.14.0 is the latest stable release "
                        "(python.org, Oct 2025)."}]
    verdict, error = orch._verify_goal("the goal", "the brief", "the output", gathered=gathered)
    assert verdict is not None and error is None
    prompt = provider.last_plan_prompt
    assert "EVIDENCE GATHERED THIS TURN" in prompt
    assert "Python 3.14.0" in prompt
    assert "WEB EVIDENCE PRECEDENCE" in prompt
    assert VERIFY_WEB_EVIDENCE_NOTE.strip() in prompt
    # Evidence must sit before the output it grounds, same convention as the context block.
    evidence_idx = prompt.index("EVIDENCE GATHERED THIS TURN")
    output_idx = prompt.index("--- WORKER OUTPUT")
    assert evidence_idx < output_idx


def test_verify_goal_web_evidence_lands_in_the_layered_tail_not_the_cached_context():
    provider = LayeredScriptedProvider(verdicts=[{"met": True, "reason": "done"}])
    orch = make_orch(provider)
    ctx = "STABLE_L2_CONTEXT"
    gathered = [{"kind": "query", "rel_path": "web_search:q",
                "text": "WEB RESULTS: the live fact."}]
    verdict, error = orch._verify_goal("the goal", "the brief", "output",
                                       context_layer=ctx, gathered=gathered)
    assert verdict is not None and error is None
    # The cached L1/L2 blocks are UNCHANGED by gathered content: only the context_layer string
    # rides there, exactly as test_verify_context_layer.py's byte-identity contract requires.
    cache_texts = cache_blocks_text(provider.verify_layers_calls[0])
    assert cache_texts == [ctx]
    # The evidence/web-note text is in the prompt as a whole (it rides the volatile tail).
    full_text = "\n".join(b["text"] for b in provider.verify_layers_calls[0])
    assert "the live fact" in full_text
    assert "WEB EVIDENCE PRECEDENCE" in full_text


def test_verify_goal_discovery_only_gathered_is_dropped_like_the_answer_path():
    # A discovery/capability listing (list_operations etc.) is a menu, not content -- the answer
    # path already drops these (_is_discovery_obs), and the verifier's evidence section must match
    # that, or it would "ground" a verdict on a menu instead of real content.
    provider = StubProvider(decisions=[{"met": True, "reason": "done"}])
    orch = make_orch(provider)
    gathered = [{"kind": "query", "rel_path": "web_search:q", "discovery": True,
                "text": "AVAILABLE CAPABILITIES: web search"}]
    verdict, error = orch._verify_goal("the goal", "the brief", "the output", gathered=gathered)
    assert verdict is not None and error is None
    assert "EVIDENCE GATHERED THIS TURN" not in provider.last_plan_prompt


# --------------------------------------------------------------------------- #
# (c) grounding_answer_tail: web-citation carve-out against REPLY_VOICE_SYSTEM
# --------------------------------------------------------------------------- #

def test_answer_tail_has_no_web_citation_note_for_an_ordinary_read():
    gathered = [{"kind": "read", "rel_path": "notes.md", "locator": "notes.md", "text": "a fact"}]
    tail = grounding_answer_tail(gathered, partial=False)
    assert "LIVE WEB result" not in tail


def test_answer_tail_has_the_web_citation_carveout_when_web_evidence_present():
    gathered = [{"kind": "query", "rel_path": "web_search:q",
                "text": "WEB RESULTS: cite facts inline as [title](url)."}]
    tail = grounding_answer_tail(gathered, partial=False)
    assert "LIVE WEB result" in tail
    assert "not retrieval metadata" in tail


def test_answer_tail_web_citation_note_absent_with_no_gathered():
    assert "LIVE WEB result" not in grounding_answer_tail([], partial=False)
    assert "LIVE WEB result" not in grounding_answer_tail(None, partial=False)
