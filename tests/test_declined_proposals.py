"""Declined proposals from earlier in the SAME conversation must reach the PLANNER, not just the
(off-by-default) overseer digest.

Before this fix, ``prior_escalations`` was only ever rendered by ``_prior_escalation_lines`` into
the overseer's digest. The overseer is off by default, so the planner never learned a proposal had
already been turned down, and a real production conversation re-fired one declined "create four
quests" proposal ten times across seventy-two messages. ``declined_proposals_block`` now feeds the
SAME history into ``context_view`` itself, so both the planner and the grounded answer see it.
"""
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    DECLINED_PROPOSALS_NOTE,
    Orchestrator,
    OrchestratorConfig,
    declined_proposals_block,
)

from .conftest import StubProvider, StubRetrieval

DECLINED_HEADER = "--- PROPOSALS THE USER ALREADY DECLINED IN THIS CONVERSATION (INTERNAL) ---"


def _orch(provider, retrieval, **kw):
    # Same isolation as test_orchestrator.py: pin the overseer off so this test measures only the
    # planner-context wiring, not the overseer's own (separately tested) behavior.
    cfg = kw.pop("config", None) or OrchestratorConfig()
    cfg.overseer = False
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), config=cfg, **kw)


def test_declined_proposal_reaches_the_planner_prompt():
    provider = StubProvider(decisions=[
        {"action": "answer", "model_tier": "haiku", "rationale": "chit-chat"},
    ])
    prior_escalations = [
        {"kind": "human", "outcome": "refused", "summary": "create four new quests"},
    ]
    _orch(provider, StubRetrieval()).run("thanks!", prior_escalations=prior_escalations)

    assert provider.plan_prompts, "planner was never called"
    prompt = provider.plan_prompts[0]
    assert DECLINED_HEADER in prompt
    assert "create four new quests" in prompt
    assert "mention this list to the user" in prompt


def test_declined_proposal_also_reaches_the_grounded_answer():
    provider = StubProvider(decisions=[
        {"action": "answer", "model_tier": "haiku", "rationale": "chit-chat"},
    ])
    prior_escalations = [
        {"kind": "human", "outcome": "declined", "summary": "send the donor email"},
    ]
    _orch(provider, StubRetrieval()).run("thanks!", prior_escalations=prior_escalations)

    assert provider.all_answer_messages, "answer was never called"
    joined = "\n".join(
        m["content"] for messages in provider.all_answer_messages for m in messages)
    assert DECLINED_HEADER in joined
    assert "send the donor email" in joined


def test_no_refused_entries_means_no_block_and_no_behavior_change():
    provider_none = StubProvider(decisions=[
        {"action": "answer", "model_tier": "haiku", "rationale": "chit-chat"},
    ])
    _orch(provider_none, StubRetrieval()).run("thanks!", prior_escalations=None)
    assert DECLINED_HEADER not in provider_none.plan_prompts[0]

    provider_met = StubProvider(decisions=[
        {"action": "answer", "model_tier": "haiku", "rationale": "chit-chat"},
    ])
    prior_escalations = [
        {"kind": "deep", "outcome": "deep_met", "summary": "wrote the one-pager"},
    ]
    _orch(provider_met, StubRetrieval()).run("thanks!", prior_escalations=prior_escalations)
    assert DECLINED_HEADER not in provider_met.plan_prompts[0]


def test_helper_is_robust_to_junk_entries():
    assert declined_proposals_block(None) == ""
    assert declined_proposals_block([]) == ""
    # Non-dict entries alongside a dict with no summary, and one with an empty/whitespace summary.
    assert declined_proposals_block([
        "not a dict", 123, None,
        {"outcome": "refused"},
        {"outcome": "refused", "summary": "   "},
    ]) == ""


def test_helper_matches_outcomes_case_insensitively_and_supports_proposal_key():
    block = declined_proposals_block([
        {"outcome": "REFUSED", "proposal": "archive the old campaign"},
    ])
    assert DECLINED_HEADER in block
    assert "archive the old campaign" in block
    assert block.endswith(DECLINED_PROPOSALS_NOTE)


def test_helper_caps_entry_count_and_summary_length():
    many = [
        {"outcome": "rejected", "summary": f"proposal number {i}"} for i in range(25)
    ]
    block = declined_proposals_block(many)
    numbered_lines = [
        line for line in block.splitlines() if line and line[0].isdigit() and ": " in line
    ]
    assert len(numbered_lines) == 10  # capped, entries 10-24 dropped
    assert "11: proposal number 10" not in block

    long_one = [{"outcome": "declined", "summary": "x" * 500}]
    block_long = declined_proposals_block(long_one)
    entry_line = block_long.splitlines()[1]  # "1: xxxx..."
    assert len(entry_line) <= len("1: ") + 200
