"""Per-idea threading, where THE IDEA IS THE CARD: the orchestrator wiring around the hub's card_thread module.

The pure parts (the fail-safe parse, the duplicate guard, the blending) are tested in the hub (tests/test_recall_card_thread.py).
What these tests pin, in the order the design demands it:

  1. OFF BY DEFAULT. With ``card_thread_enabled`` unset, the planner prompt carries no TOPIC block,
     the decide-tool schema has no ``card_thread`` field, a stray ``card_thread`` in a response is
     ignored, no EVENT_CARD_THREAD is emitted, and the result carries no thread. Another consumer
     cannot be affected by a feature it never asked for.
  2. ZERO EXTRA LLM CALLS. The topic rides the planning call the orchestrator already makes, and the
     candidate PRIOR is built from the cards this turn's retrieval already scored.
  3. THE FAIL-SAFE. Any parse failure, any ambiguity, any unknown card id: CONTINUE the current card.
  4. PRIORITY BLENDING, NOT ISOLATION. This card's floor plus a small global floor; other ideas stay
     reachable behind a penalty.
  5. A TOPIC SWITCH IS NEVER A MODE SIGNAL. The brainstorm latch does not move when the idea does.
"""
import pytest

from quest_ai_runner.core.adapters_values import PlanDecision
from quest_ai_runner.core.context_doctrine import CARD_THREAD_GATE
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    DECIDE_TOOL,
    Orchestrator,
    OrchestratorConfig,
    decide_tool_for,
    normalize_decision,
)

from .conftest import StubProvider, StubRetrieval


# --------------------------------------------------------------------------------------------
# 1 + 2 + 5: the orchestrator wiring, driven through a stub provider (no network).
# --------------------------------------------------------------------------------------------

class _StubAssembler:
    """The turn's retrieval. Its card_metadata IS the prior: no second search is ever made."""

    def __init__(self, card_metadata):
        self.card_metadata = card_metadata
        self.calls = 0
        self.meta_seen: dict = {}

    def assemble(self, task_text, *, meta=None):
        from hubofhubs.ports.context import AssembledContext
        self.calls += 1
        self.meta_seen = dict(meta or {})
        return AssembledContext(context_view="## cards\n(the assembled cards)",
                                card_metadata=[dict(c) for c in self.card_metadata])

    def record(self, task_text, outcome):
        pass


class _CollectSink:
    """A ProgressSink that keeps every event (see core.adapters_bases.ProgressSinkBase.update)."""

    def __init__(self):
        self.events = []

    def update(self, event, mode):
        self.events.append(event)


def _orch(provider, *, card_thread_enabled=False, assembler=None, max_steps=2, **cfg_kwargs):
    # overseer off: these tests assert the card thread costs ZERO extra LLM calls, and the overseer
    # (ON by default in the library) would add its own consults to those counts. Its behavior is
    # covered in test_overseer.py.
    cfg = OrchestratorConfig(max_steps=max_steps, card_thread_enabled=card_thread_enabled,
                             instant_ack=False, narrate=False, verify_claims=False,
                             answer_goal_max_iterations=1, overseer=False, **cfg_kwargs)
    return Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=cfg,
                        context_assembler=assembler)


CARDS = [{"id": "topic:launch-plan", "title": "Launch plan", "card_type": "topic"}]


def test_off_by_default_the_feature_is_invisible():
    """A consumer that never opted in must see a byte-identical prompt, schema, and result."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "r", "model_tier": "sonnet",
                                        "card_thread": "switch_to:topic:launch-plan"}])
    orch = _orch(provider, assembler=_StubAssembler(CARDS))
    sink = _CollectSink()
    res = orch.run("what about the launch plan?", sink=sink)

    prompt = provider.plan_prompts[0]
    assert "TOPIC (`card_thread`" not in prompt          # no doctrine block
    assert "CURRENT TOPIC" not in prompt                 # no candidate prior
    assert "card_thread" not in provider.plan_tool_schemas[0]["input_schema"]["properties"]
    assert res.card_thread is None                       # a stray field in the response is ignored
    assert not [e for e in sink.events if e.type == "card_thread"]


def test_topic_rides_the_planning_call_the_orchestrator_already_makes():
    """ZERO extra LLM calls: the topic costs no call of its own, and the prior costs no search."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "r", "model_tier": "sonnet",
                                        "card_thread": "switch_to:topic:launch-plan"}])
    assembler = _StubAssembler(CARDS)
    orch = _orch(provider, card_thread_enabled=True, assembler=assembler)
    sink = _CollectSink()
    res = orch.run(
        "back to the launch plan",
        sink=sink,
        card_thread={"active_card_id": "topic:sleep", "active_label": "Sleep",
                     "allowed_types": ["topic"],
                     "candidates": [{"id": "topic:general", "label": "General chat"}]},
    )

    assert provider.plan_calls == 1        # one planning call, the one it always makes
    assert assembler.calls == 1            # and no second retrieval: the prior IS the assembly

    prompt = provider.plan_prompts[0]
    assert "TOPIC (`card_thread`" in prompt
    assert "CURRENT TOPIC: [topic:sleep] Sleep" in prompt
    assert "[topic:launch-plan] Launch plan" in prompt   # the free prior, from retrieval
    assert "[topic:general] General chat" in prompt      # the consumer's always-offered card
    assert "card_thread" in provider.plan_tool_schemas[0]["input_schema"]["properties"]

    assert res.card_thread == {"action": "switch", "card_id": "topic:launch-plan",
                               "label": None, "raw": "switch_to:topic:launch-plan",
                               "fell_back": False}
    ev = [e for e in sink.events if e.type == "card_thread"]
    assert len(ev) == 1
    assert ev[0].data["card_id"] == "topic:launch-plan"
    assert ev[0].data["previous_card_id"] == "topic:sleep"
    # The active card reaches a card-aware assembler as meta (priority blending at retrieval).
    assert assembler.meta_seen.get("thread_card_id") == "topic:sleep"


def test_a_planner_failure_keeps_the_thread_rather_than_losing_it():
    class _Boom(StubProvider):
        def plan(self, prompt, *, model, tool_schema):
            self.plan_calls += 1
            raise RuntimeError("planner down")

    provider = _Boom(decisions=[])
    orch = _orch(provider, card_thread_enabled=True, assembler=_StubAssembler(CARDS))
    res = orch.run("anything", card_thread={"active_card_id": "topic:sleep"})

    assert res.card_thread["action"] == "continue"
    assert res.card_thread["card_id"] == "topic:sleep"
    assert res.card_thread["fell_back"] is True


def test_a_new_topic_leaves_the_card_id_to_the_consumer():
    """The library owns no card store, so it never mints an id. It reports the label; the consumer
    creates (or dedupes onto) the card."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "r", "model_tier": "sonnet",
                                        "card_thread": "new:Sleep experiment"}])
    orch = _orch(provider, card_thread_enabled=True, assembler=_StubAssembler(CARDS))
    res = orch.run("I want to try polyphasic sleep",
                   card_thread={"active_card_id": "topic:launch-plan"})

    assert res.card_thread["action"] == "new"
    assert res.card_thread["label"] == "Sleep experiment"
    assert res.card_thread["card_id"] is None


def test_a_topic_switch_never_flips_the_brainstorm_latch():
    """SETTLED: a topic shift is not a mode signal. A held conversation stays held when the idea
    moves, and returning to an old idea does not release the hold."""
    provider = StubProvider(decisions=[{"action": "deep", "goal": "do the thing", "deep_brief": "b",
                                        "rationale": "r", "model_tier": "sonnet",
                                        "card_thread": "switch_to:topic:launch-plan"}])
    orch = _orch(provider, card_thread_enabled=True, assembler=_StubAssembler(CARDS),
                 mode_signals_enabled=True, execution_mode="brainstorm")
    # The release judge is the exit authority; hold it (its own fail-safe) so the only thing that
    # could move the latch this turn is the topic switch. It must not.
    orch.judge_brainstorm_release = lambda user_message, transcript="": (False, "held")
    sink = _CollectSink()
    res = orch.run("back to the launch plan", sink=sink,
                   card_thread={"active_card_id": "topic:sleep",
                                "candidates": [{"id": "topic:launch-plan", "label": "Launch plan"}]})

    assert res.card_thread["action"] == "switch"      # the idea moved
    assert res.mode_signal is None                    # the latch did not
    assert res.kind == "answer"                       # still held: the planner's "deep" was degraded
    assert not [e for e in sink.events if e.type == "mode_signal"]


def test_the_lifecycle_doctrine_reaches_the_reply_when_threading_is_on():
    """A completed quest can surface as context on any turn. The reply must be told to treat it as
    knowledge, not open work, and that instruction rides the SYSTEM prompt (the layer an instruction
    the reply must OBEY belongs in), not the grounding block it is told never to mention."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "r", "model_tier": "sonnet",
                                        "card_thread": "continue"}])
    orch = _orch(provider, card_thread_enabled=True, assembler=_StubAssembler(CARDS))
    orch.run("how did the marathon quest go?", card_thread={"active_card_id": "quest:abc"})

    systems = [s for s in provider.answer_systems if s]
    assert systems, "the answer call carried no system prompt"
    assert any("FINISHED WORK IS KNOWLEDGE, NOT A TO DO" in str(s) for s in systems)

    # ...and with threading off, nothing is added (the default consumer is untouched).
    plain_provider = StubProvider(decisions=[{"action": "answer", "rationale": "r",
                                              "model_tier": "sonnet"}])
    _orch(plain_provider, assembler=_StubAssembler(CARDS)).run("hi")
    assert not any("FINISHED WORK IS KNOWLEDGE" in str(s) for s in plain_provider.answer_systems)


def test_normalize_decision_ignores_the_field_unless_the_consumer_opted_in():
    raw = {"action": "answer", "rationale": "r", "card_thread": "new:whatever"}
    assert normalize_decision(raw, OrchestratorConfig()).card_thread is None
    assert normalize_decision(
        raw, OrchestratorConfig(card_thread_enabled=True)).card_thread == "new:whatever"
    # A non-string is dropped rather than trusted.
    assert normalize_decision(
        {"action": "answer", "rationale": "r", "card_thread": 7},
        OrchestratorConfig(card_thread_enabled=True)).card_thread is None
    assert isinstance(PlanDecision(action="answer").card_thread, type(None))


def test_the_schema_only_grows_the_field_when_asked():
    assert "card_thread" not in decide_tool_for(False, False)["input_schema"]["properties"]
    assert "card_thread" not in decide_tool_for(True, True)["input_schema"]["properties"]
    tool = decide_tool_for(True, True, True)
    assert "card_thread" in tool["input_schema"]["properties"]
    # REQUIRED when it is offered: an optional field is one a model quietly omits, and every
    # omission lands on the fail-safe, so the topic would never move (a real run left three
    # different ideas all sitting on the first card the user opened).
    assert "card_thread" in tool["input_schema"]["required"]
    assert "card_thread" not in DECIDE_TOOL["input_schema"].get("required", [])
    # ...and adding it never disturbs the fields already there.
    assert "mode_signal" in tool["input_schema"]["properties"]
    assert tool["input_schema"]["properties"]["deferred_deep"]["description"].startswith(
        "When action='answer', optionally specify deep work to hand to the background task queue")


def test_a_silent_first_plan_does_not_freeze_the_turn_on_the_wrong_idea():
    """A REAL failure, pinned. The field is required, but a model still returns nothing for it when
    the turn is busy: on a "back to the launch plan" turn the planner omitted the topic while it was
    planning reads, the fail-safe continued, and the turn was filed under the idea the user had just
    said they were LEAVING. A fell-back decision is therefore not an answer: a later plan step in the
    same turn may still supply the real one."""
    provider = StubProvider(decisions=[
        # step 1: busy planning a read, and silent about the topic.
        {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "look first",
         "model_tier": "sonnet"},
        # step 2: now it says what the message was actually about.
        {"action": "answer", "rationale": "got it", "model_tier": "sonnet",
         "card_thread": "switch_to:topic:launch-plan"},
    ])
    orch = _orch(provider, card_thread_enabled=True, assembler=_StubAssembler(CARDS), max_steps=3)
    sink = _CollectSink()
    res = orch.run("back to the launch plan", sink=sink,
                   card_thread={"active_card_id": "topic:general", "active_label": "General chat"})

    assert res.card_thread["action"] == "switch"
    assert res.card_thread["card_id"] == "topic:launch-plan"
    assert res.card_thread["fell_back"] is False
    # The consumer must not act on the fail-safe and then again on the real answer as if they were
    # two different turns: the LAST event carries the decision that stands.
    events = [e for e in sink.events if e.type == "card_thread"]
    assert events[-1].data["card_id"] == "topic:launch-plan"


def test_a_turn_that_never_expresses_a_topic_keeps_the_fail_safe():
    """...and when NO step ever supplies one, the standing rule holds: continue the current card."""
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"rel_path": "README.md"}], "rationale": "r",
         "model_tier": "sonnet"},
        {"action": "answer", "rationale": "r", "model_tier": "sonnet"},
    ])
    orch = _orch(provider, card_thread_enabled=True, assembler=_StubAssembler(CARDS), max_steps=3)
    res = orch.run("anything at all", card_thread={"active_card_id": "topic:general"})

    assert res.card_thread == {"action": "continue", "card_id": "topic:general", "label": None,
                               "raw": "", "fell_back": True}


# --------------------------------------------------------------------------------------------
# THE INDEPENDENT-RECALL TEST -- a sub-decision inside a plan (pricing, timeline, a vendor pick)
# often SOUNDS like its own subject without BEING independently recallable. This pins the gate's
# doctrine text to the concrete test (not a "match on meaning" vibe) and a couple of its worked
# examples end to end, so a future edit cannot silently drop the test or water it back down.
# --------------------------------------------------------------------------------------------

def test_the_gate_text_states_the_independent_recall_test_not_a_meaning_match_vibe():
    """PINNED: the vague 'match on meaning, not on shared words' instruction was replaced by a
    concrete test (would this still be worth looking up if the current card were deleted), because
    it gave no real answer for a sub-decision that sounds like its own subject (pricing, timeline)
    without being one. Guard both the removal and the replacement."""
    assert "match on meaning" not in CARD_THREAD_GATE.lower()
    assert "card were deleted" in CARD_THREAD_GATE
    assert "sub-decision" in CARD_THREAD_GATE
    assert "its own future" in CARD_THREAD_GATE
    # The worked examples that motivated the fix are present as few-shot guidance.
    assert "charge for the launch" in CARD_THREAD_GATE
    assert "Sarah's onboarding" in CARD_THREAD_GATE
    assert "Q3 budget" in CARD_THREAD_GATE


def test_a_pricing_sub_decision_continues_the_launch_card_not_a_new_one():
    """Worked example 1: 'what should we charge?' mid launch-planning is a sub-decision that only
    ever gets looked up in service of the launch, so it must continue rather than open its own card
    even though "pricing" shares no words with "launch"."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "r", "model_tier": "sonnet",
                                        "card_thread": "continue"}])
    orch = _orch(provider, card_thread_enabled=True, assembler=_StubAssembler(CARDS))
    res = orch.run("what should we charge for the launch?",
                   card_thread={"active_card_id": "topic:launch-plan", "active_label": "Launch plan"})

    assert res.card_thread["action"] == "continue"
    assert res.card_thread["card_id"] == "topic:launch-plan"


def test_a_different_persons_progress_opens_a_new_card_mid_launch_planning():
    """Worked example 2: asked mid launch-planning, 'how's Sarah's onboarding going' has its own
    independent trajectory (it would be asked about even if the launch never happened), so it must
    open a new card rather than continue the launch card just because it is the current one."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "r", "model_tier": "sonnet",
                                        "card_thread": "new:Sarah's onboarding"}])
    orch = _orch(provider, card_thread_enabled=True, assembler=_StubAssembler(CARDS))
    res = orch.run("how's Sarah's onboarding going",
                   card_thread={"active_card_id": "topic:launch-plan", "active_label": "Launch plan"})

    assert res.card_thread["action"] == "new"
    assert res.card_thread["label"] == "Sarah's onboarding"


def test_a_signposted_unrelated_ask_opens_a_new_card():
    """Worked example 3: an explicit signpost ('separately') plus a subject unconnected to whether
    the launch succeeds or fails -- a new card, not a continue."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "r", "model_tier": "sonnet",
                                        "card_thread": "new:Q3 budget"}])
    orch = _orch(provider, card_thread_enabled=True, assembler=_StubAssembler(CARDS))
    res = orch.run("separately, can you check on the Q3 budget",
                   card_thread={"active_card_id": "topic:launch-plan", "active_label": "Launch plan"})

    assert res.card_thread["action"] == "new"
    assert res.card_thread["label"] == "Q3 budget"
