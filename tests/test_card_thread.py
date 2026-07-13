"""Per-idea threading, where THE IDEA IS THE CARD (core/card_thread.py + the orchestrator wiring).

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

from quest_ai_runner.core.adapters import PlanDecision
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.card_thread import (
    ACTION_CONTINUE,
    ACTION_NEW,
    ACTION_SWITCH,
    CardCandidate,
    CardThreadContext,
    find_duplicate_label,
    lifecycle_note,
    merge_candidates,
    normalize_label,
    parse_card_thread,
    penalized_budget,
    rank_card_first,
    render_thread_hint,
    select_thread_floor,
    split_by_card,
)
from quest_ai_runner.core.orchestrator import (
    DECIDE_TOOL,
    Orchestrator,
    OrchestratorConfig,
    decide_tool_for,
    normalize_decision,
)

from .conftest import StubProvider, StubRetrieval


# --------------------------------------------------------------------------------------------
# 3. THE FAIL-SAFE: an ambiguous topic assignment must never cost a turn.
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    None, "", "   ", 42, {"action": "switch"}, ["switch_to:x"],
    "switch_to:", "new:", "resume", "switch:card-a", "CONTINUE ALL", "card-a",
])
def test_any_garbage_continues_the_current_card(raw):
    d = parse_card_thread(raw, active_card_id="card-a", known_ids=["card-a", "card-b"])
    assert d.action == ACTION_CONTINUE
    assert d.card_id == "card-a"
    assert d.fell_back is True


def test_a_dropped_namespace_prefix_is_repaired_when_it_is_unambiguous():
    """A REAL failure, pinned. A model handed "quest:quest_rt_622dc4ed" verbatim emitted
    "switch_to:quest_rt_622dc4ed", losing the namespace. The id was unknown, the fail-safe fired,
    and the turn stayed on the wrong idea. An EXACT, unique match on the id's trailing segment is
    not a guess, so it is repaired. Two matches would be an ambiguity, and ambiguity continues."""
    d = parse_card_thread("switch_to:quest_rt_622dc4ed", active_card_id="topic:general",
                          known_ids=["topic:general", "quest:quest_rt_622dc4ed"])
    assert (d.action, d.card_id, d.fell_back) == (ACTION_SWITCH, "quest:quest_rt_622dc4ed", False)

    # Ambiguous (two known ids share the trailing segment): continue, never pick one.
    d = parse_card_thread("switch_to:plan", active_card_id="topic:general",
                          known_ids=["topic:plan", "quest:plan"])
    assert (d.action, d.card_id, d.fell_back) == (ACTION_CONTINUE, "topic:general", True)


def test_switch_to_an_unknown_card_continues_rather_than_inventing_one():
    """The planner named a card that is not on the table. Do NOT create it, do NOT guess a
    near-match: continue. Inventing a card from a hallucinated id is how a card space rots."""
    d = parse_card_thread("switch_to:card-zzz", active_card_id="card-a",
                          known_ids=["card-a", "card-b"])
    assert (d.action, d.card_id, d.fell_back) == (ACTION_CONTINUE, "card-a", True)


def test_continue_switch_and_new_parse():
    assert parse_card_thread("continue", active_card_id="card-a").action == ACTION_CONTINUE

    d = parse_card_thread("switch_to:card-b", active_card_id="card-a",
                          known_ids=["card-a", "card-b"])
    assert (d.action, d.card_id, d.fell_back) == (ACTION_SWITCH, "card-b", False)

    d = parse_card_thread("new:Launch plan for v2", active_card_id="card-a")
    assert (d.action, d.label, d.card_id, d.fell_back) == (ACTION_NEW, "Launch plan for v2",
                                                           None, False)


def test_switching_to_the_card_already_active_is_a_continue():
    """Not a rename of the same thing: switch and continue mean different things to the consumer
    (a switch moves the conversation's active card), so a self-switch must land as continue."""
    d = parse_card_thread("switch_to:card-a", active_card_id="card-a", known_ids=["card-a"])
    assert d.action == ACTION_CONTINUE and d.card_id == "card-a"


# --------------------------------------------------------------------------------------------
# The dedupe guard: "new:" must not litter the card space with twins.
# --------------------------------------------------------------------------------------------

def test_dedupe_guard_catches_the_obvious_twins():
    existing = [CardCandidate(id="topic:launch-plan", label="Launch plan"),
                CardCandidate(id="topic:sleep", label="Sleep")]
    assert find_duplicate_label("launch plan", existing) == "topic:launch-plan"
    assert find_duplicate_label("The Launch Plan!", existing) == "topic:launch-plan"
    # Multi-word containment either way is the same idea.
    assert find_duplicate_label("launch plan for v2", existing) == "topic:launch-plan"
    # A single shared word is a CATEGORY, not the same idea (the specificity discipline).
    assert find_duplicate_label("launch metrics", existing) is None
    assert find_duplicate_label("marathon training", existing) is None
    assert normalize_label("  The  Launch-Plan!! ") == "the launch plan"


# --------------------------------------------------------------------------------------------
# 2. THE PRIOR is free: it is built from the cards retrieval ALREADY scored this turn.
# --------------------------------------------------------------------------------------------

def test_merge_candidates_uses_this_turns_retrieval_and_respects_allowed_types():
    ctx = CardThreadContext(
        active_card_id="topic:sleep", active_label="Sleep",
        candidates=[CardCandidate(id="topic:general", label="General chat")],
        allowed_types=["topic", "quest"],
    )
    card_metadata = [
        {"id": "topic:launch-plan", "title": "Launch plan", "card_type": "topic"},
        {"id": "grant:team:1:quest_command.goal.create", "title": "grant", "card_type": "capability"},
        {"id": "quest:abc", "title": "Marathon quest", "card_type": "quest", "lifecycle": "completed"},
        {"id": "docs:onboarding", "title": "Docs", "card_type": ""},   # untyped: not a topic
    ]
    cands = merge_candidates(ctx, card_metadata)
    ids = [c.id for c in cands]

    assert "topic:general" in ids          # consumer candidates always pass
    assert "topic:launch-plan" in ids      # keyword/vector arm surfaced it: the free prior
    assert "quest:abc" in ids
    assert "grant:team:1:quest_command.goal.create" not in ids   # not a topic
    assert "docs:onboarding" not in ids
    # The ACTIVE card is always on the table even when nothing retrieved it this turn ("make it
    # shorter" carries none of its keywords).
    assert "topic:sleep" in ids
    assert next(c for c in cands if c.id == "quest:abc").status == "completed"


def test_thread_hint_names_the_active_card_the_candidates_and_finished_work():
    ctx = CardThreadContext(active_card_id="topic:sleep", active_label="Sleep")
    hint = render_thread_hint(ctx, [
        CardCandidate(id="topic:launch-plan", label="Launch plan", why="matched this message"),
        CardCandidate(id="quest:abc", label="Marathon quest", status="completed"),
    ])
    assert "CURRENT TOPIC: [topic:sleep] Sleep" in hint
    assert "[topic:launch-plan] Launch plan" in hint
    assert "completed" in hint


def test_lifecycle_note_marks_finished_work_as_knowledge_not_a_to_do():
    note = lifecycle_note("completed", "in June")
    assert "completed (in June)" in note
    assert "do not propose working it as if it were still open" in note
    assert "—" not in note                       # no em dashes in user-facing text
    assert lifecycle_note("active") == ""        # ongoing work says nothing


# --------------------------------------------------------------------------------------------
# 4. PRIORITY BLENDING: this card's floor + a small global floor; siblings reachable, not resident.
# --------------------------------------------------------------------------------------------

def _msgs():
    """An interleaved conversation: idea A, idea B, idea A, idea C, back to A."""
    return [
        {"role": "user", "content": "A1", "card_id": "A"},
        {"role": "assistant", "content": "A1r", "card_id": "A"},
        {"role": "user", "content": "B1", "card_id": "B"},
        {"role": "assistant", "content": "B1r", "card_id": "B"},
        {"role": "user", "content": "A2", "card_id": "A"},
        {"role": "assistant", "content": "A2r", "card_id": "A"},
        {"role": "user", "content": "C1", "card_id": "C"},
        {"role": "assistant", "content": "C1r", "card_id": "C"},
    ]


def test_floor_is_this_cards_turns_plus_a_small_global_floor():
    floor = select_thread_floor(_msgs(), card_id="A", card_turns=8, global_turns=2)
    got = [m["content"] for m in floor]

    # Every turn of idea A is there, in conversation order.
    assert got[:4] == ["A1", "A1r", "A2", "A2r"]
    # The very last exchange rides the GLOBAL floor even though it belongs to another idea: that is
    # what makes "as I just said" survive an interleave. This is priority blending, not isolation.
    assert got[-2:] == ["C1", "C1r"]
    # But idea B, which is older than the global floor, is NOT resident in A's floor.
    assert "B1" not in got and "B1r" not in got


def test_an_unstamped_message_always_belongs(regression="pre-threading conversations keep their floor"):
    """A message with no card_id predates threading (or came from a surface that does not stamp).
    Dropping it would silently shrink an existing conversation's floor, so the fail-safe is KEEP."""
    msgs = [{"role": "user", "content": "old", "card_id": None},
            {"role": "user", "content": "B1", "card_id": "B"},
            {"role": "user", "content": "A1", "card_id": "A"}]
    got = [m["content"] for m in select_thread_floor(msgs, card_id="A", card_turns=8,
                                                     global_turns=1)]
    assert "old" in got and "A1" in got
    mine, others = split_by_card(msgs, card_id="A")
    assert [m["content"] for m in mine] == ["old", "A1"]
    assert [m["content"] for m in others] == ["B1"]


def test_recall_ranks_this_card_first_but_a_strong_sibling_still_gets_through():
    """The penalty is not a filter: "combine those two ideas" has to keep working."""
    items = [
        {"id": "sibling-strong", "card_id": "B", "score": 0.9},
        {"id": "mine-weak", "card_id": "A", "score": 0.5},
        {"id": "sibling-weak", "card_id": "B", "score": 0.3},
        {"id": "mine-strong", "card_id": "A", "score": 0.8},
    ]
    ranked = rank_card_first(items, card_id="A", get_card_id=lambda i: i["card_id"],
                             get_score=lambda i: i["score"], penalty=0.5)
    ids = [i["id"] for i in ranked]
    assert ids[0] == "mine-strong"          # 0.8 vs the sibling's penalized 0.45
    assert ids[1] == "mine-weak"            # 0.5 still beats the penalized 0.45
    assert "sibling-strong" in ids          # reachable, never filtered out
    assert ids.index("sibling-strong") < ids.index("sibling-weak")
    assert penalized_budget(1000, 0.5) == 500 and penalized_budget(-5) == 0


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
        from quest_ai_runner.core.adapters import AssembledContext
        self.calls += 1
        self.meta_seen = dict(meta or {})
        return AssembledContext(context_view="## cards\n(the assembled cards)",
                                card_metadata=[dict(c) for c in self.card_metadata])

    def record(self, task_text, outcome):
        pass


class _CollectSink:
    """A ProgressSink that keeps every event (see core.adapters.ProgressSinkBase.update)."""

    def __init__(self):
        self.events = []

    def update(self, event, mode):
        self.events.append(event)


def _orch(provider, *, card_thread_enabled=False, assembler=None, max_steps=2, **cfg_kwargs):
    cfg = OrchestratorConfig(max_steps=max_steps, card_thread_enabled=card_thread_enabled,
                             instant_ack=False, narrate=False, verify_claims=False,
                             answer_goal_max_iterations=1, **cfg_kwargs)
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
