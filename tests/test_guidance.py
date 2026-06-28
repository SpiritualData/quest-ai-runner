"""GuidanceProvider: the optional use-case-specific instruction adapter.

These prove (a) with guidance=None the orchestrator behaves exactly as before (no APPLICABLE
GUIDANCE block, no errors), (b) the list_guidance / read_guidance verbs return the expected
Observations, (c) select() pre-selection prepends the guidance block, and (d) read_guidance for
an already-selected id returns the de-dupe note. All offline — no network, no API key.
"""
from typing import List, Optional

from quest_ai_runner.core.adapters import GuidanceCard, GuidanceProviderBase
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator

from .conftest import StubProvider, StubRetrieval


class FakeGuidance(GuidanceProviderBase):
    """In-memory GuidanceProvider over a fixed set of cards. ``select`` returns cards whose
    relevance text shares a keyword with the message (a trivial, deterministic stand-in for a
    real semantic selector)."""

    def __init__(self, cards: List[GuidanceCard], *, select_ids: Optional[List[str]] = None):
        self._cards = {c.id: c for c in cards}
        # When select_ids is given, select() returns exactly those (regardless of message); else
        # it returns []. Keeps the pre-selection test fully deterministic.
        self._select_ids = select_ids
        self.list_calls = 0
        self.read_calls: List[str] = []
        self.select_calls = 0

    def list(self) -> List[GuidanceCard]:
        self.list_calls += 1
        # Catalog: id + title + relevance, body EMPTY (cheap).
        return [GuidanceCard(id=c.id, title=c.title, relevance=c.relevance, body="")
                for c in self._cards.values()]

    def read(self, card_id: str) -> Optional[GuidanceCard]:
        self.read_calls.append(card_id)
        return self._cards.get(card_id)

    def select(self, user_message, *, task_type=None, rep_id=None, team_id=None, org_id=None,
               operation=None, function_name=None, tags=None, limit=5) -> List[GuidanceCard]:
        self.select_calls += 1
        if not self._select_ids:
            return []
        return [self._cards[i] for i in self._select_ids[:limit] if i in self._cards]


def _orch(provider, retrieval, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), **kw)


def _cards():
    return [
        GuidanceCard(id="quest_creation", title="Creating a quest",
                     relevance="the user wants to start a new quest or goal",
                     body="Ask for the outcome, then propose milestones."),
        GuidanceCard(id="daily_reflection", title="Daily reflection",
                     relevance="the user is doing a daily check-in",
                     body="Parse the plan into goals with suggested times."),
    ]


# --- (a) guidance=None is byte-for-byte the prior behavior --------------------------------

def test_no_guidance_is_unchanged_behavior():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    res = _orch(provider, StubRetrieval()).run("hello")
    assert res.kind == "answer"
    # No guidance wired → no APPLICABLE GUIDANCE block anywhere in the planner context.
    assert "--- APPLICABLE GUIDANCE ---" not in provider.plan_prompts[0]


def test_list_read_guidance_verbs_inert_without_provider():
    # Even if the planner asks for guidance verbs, an orchestrator with guidance=None returns a
    # benign Observation and answers — never raises.
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"list_guidance": True}], "rationale": "try"},
        {"action": "read", "reads": [{"read_guidance": "x"}], "rationale": "try"},
        {"action": "answer", "rationale": "done"},
    ])
    res = _orch(provider, StubRetrieval()).run("what guidance is there?")
    assert res.kind == "answer"
    joined = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "No guidance is available" in joined


# --- (b) the verbs return the expected Observations ---------------------------------------

def test_list_guidance_returns_catalog():
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"list_guidance": True}], "rationale": "catalog"},
        {"action": "answer", "rationale": "now I know"},
    ])
    g = FakeGuidance(_cards())
    res = _orch(provider, StubRetrieval(), guidance=g).run("what can you help with?")
    assert res.kind == "answer"
    assert g.list_calls == 1
    # The catalog is a capability MENU: it informs the PLANNER (so the brain knows which cards it
    # could read_guidance) but is kept out of the answer grounding, so the brain reads the matching
    # card for real content before answering rather than answering from the menu.
    planner_view = "\n".join(provider.plan_prompts)
    # Catalog carries id + title + relevance (no body).
    assert "quest_creation" in planner_view and "Creating a quest" in planner_view
    assert "Ask for the outcome" not in planner_view    # body is NOT in the catalog
    answer = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "quest_creation" not in answer               # menu is excluded from the answer


def test_read_guidance_returns_card_body():
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"read_guidance": "daily_reflection"}], "rationale": "read"},
        {"action": "answer", "rationale": "done"},
    ])
    g = FakeGuidance(_cards())
    res = _orch(provider, StubRetrieval(), guidance=g).run("help me reflect")
    assert res.kind == "answer"
    assert g.read_calls == ["daily_reflection"]
    joined = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "Parse the plan into goals" in joined        # the body reached the grounding


def test_read_guidance_unknown_id_is_benign():
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"read_guidance": "nope"}], "rationale": "read"},
        {"action": "answer", "rationale": "done"},
    ])
    g = FakeGuidance(_cards())
    res = _orch(provider, StubRetrieval(), guidance=g).run("x")
    assert res.kind == "answer"
    joined = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "No guidance card with id 'nope'" in joined


# --- (c) select() pre-selection prepends the guidance block --------------------------------

def test_select_prepends_applicable_guidance_block():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    g = FakeGuidance(_cards(), select_ids=["quest_creation"])
    res = _orch(provider, StubRetrieval(), guidance=g).run("I want to start a new quest")
    assert res.kind == "answer"
    assert g.select_calls == 1
    # The pre-selected card (title + relevance + body) is in the planner context, under the block.
    step1 = provider.plan_prompts[0]
    assert "--- APPLICABLE GUIDANCE ---" in step1
    assert "Creating a quest" in step1
    assert "Ask for the outcome" in step1               # the body is included on pre-selection
    # And it reaches the final answer grounding too.
    joined = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "--- APPLICABLE GUIDANCE ---" in joined


def test_select_empty_adds_no_block():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    g = FakeGuidance(_cards())                            # select() returns []
    res = _orch(provider, StubRetrieval(), guidance=g).run("unrelated chit-chat")
    assert res.kind == "answer"
    assert g.select_calls == 1
    assert "--- APPLICABLE GUIDANCE ---" not in provider.plan_prompts[0]


# --- (d) read_guidance for an already-selected id returns the de-dupe note -----------------

def test_read_guidance_of_preselected_id_returns_dedupe_note():
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"read_guidance": "quest_creation"}], "rationale": "read"},
        {"action": "answer", "rationale": "done"},
    ])
    g = FakeGuidance(_cards(), select_ids=["quest_creation"])
    res = _orch(provider, StubRetrieval(), guidance=g).run("start a new quest")
    assert res.kind == "answer"
    # The card was pre-selected, so read_guidance of the same id must NOT re-fetch the body.
    assert g.read_calls == []
    joined = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "already provided above under APPLICABLE GUIDANCE" in joined
