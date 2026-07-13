"""The REFERENCE-REUSE LOOP: a deep run explores an environment once, and the NEXT deep run on the
same area (even from another conversation) is HANDED what it found instead of exploring again.

The links pinned here, end to end:
  1. a deep run's future context becomes typed, resolvable card REFERENCES (the updater's edits);
  2. a LEARNED card (name/description + reference content, no bootstrapped summary/files) is
     RETRIEVABLE: it is scored on its name, description, and the paths its references point at, so
     it clears the confidence gate instead of sinking under it;
  3. a selected card's references RENDER into a deep worker's brief, NAMING what they point at, so
     the worker starts with the paths;
  4. retrieval for a deep run is USER-scoped, never conversation-scoped: a second conversation gets
     the first one's references;
  5. an unresolvable (moved/deleted) reference degrades to a pointer line, never an error;
  6. PER-SOURCE usage recency: a source that is actually rendered is warmed, one that is merely held
     goes cold, and the hot ones win the render budget, without churning the rendered bytes.
"""
from typing import Any, Dict, List, Optional

from quest_ai_runner.adapters.card_content_render import (
    locator_label,
    mark_items_used,
    normalize_content,
    rank_content_by_recency_relevance,
    render_block_lines,
    tokenize,
)
from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig, PlanDecision

from .conftest import StubRetrieval


# --------------------------------------------------------------------------- fixtures


def _project(tmp_path):
    """A small external environment: a config, a module with the thing buried in it, a README."""
    (tmp_path / "src" / "telemetry").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "src" / "telemetry" / "correction.py").write_text(
        "def correct_clock_skew(packet, settings):\n    return packet\n"
    )
    (tmp_path / "config" / "relay.toml").write_text("[skew]\nmax_skew_ms = 750\n")
    (tmp_path / "README.md").write_text("# Relay\n\nCorrects clock skew.\n")
    return tmp_path


def _learned_card(cards_dir, *, card_id="learned-skew", extra_items=None):
    """The card an updater writes from a deep run's future context: NO summary, NO files entries,
    knowledge held entirely as typed file REFERENCES."""
    items = [
        {"id": "f1", "type": "file", "locator": {"path": "src/telemetry/correction.py"},
         "ts": 100.0, "why": "the clock-skew algorithm lives here"},
        {"id": "f2", "type": "file", "locator": {"path": "config/relay.toml"},
         "ts": 100.0, "why": "the [skew] tuning values"},
    ]
    items.extend(extra_items or [])
    card = {
        "id": card_id,
        "name": "Relay clock-skew correction",
        "description": "Where clock-skew correction lives in the relay and what tunes it.",
        "content": items,
    }
    import json
    (cards_dir).mkdir(parents=True, exist_ok=True)
    (cards_dir / f"{card_id}.json").write_text(json.dumps(card))
    return card


class _CapturingRunner:
    """A DeepRunner that records the brief and context_preamble it was handed."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal, brief, model=None, max_turns=None,
                 context_preamble=None, **kw) -> DeepResult:
        self.calls.append({"goal": goal, "brief": brief, "context_preamble": context_preamble or ""})
        return DeepResult(met=True, output="done")


class _Provider:
    """Planner + verifier only (no card filter): keeps retrieval deterministic and offline."""

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if tool_schema.get("name") == "goal_verdict":
            return {"met": True}
        return {"action": "deep", "goal": "g", "rationale": "r"}

    def answer(self, messages, *, model, system=None) -> str:
        return "ANSWER"

    def list_models(self) -> List[str]:
        return ["claude-sonnet-4-6"]


def _orch(store, runner, provider=None):
    provider = provider or _Provider()
    return Orchestrator(
        retrieval=StubRetrieval({}),
        provider=provider,
        registry=ModelRegistry(provider),
        deep_runner=runner,
        config=OrchestratorConfig(deep_goal_max_iterations=1, deep_model_ladder=["sonnet"],
                                  async_card_update=False),
        context_assembler=store,
    )


def _decoy_cards(cards_dir):
    """Bootstrapped file cards on OTHER topics: a learned card has to win against them, and their
    presence makes the IDF/gate arithmetic the real one (a single-card store has no IDF signal)."""
    import json
    cards_dir.mkdir(parents=True, exist_ok=True)
    for cid, summary, path in (
        ("packet-parsing", "Parses a raw wire record into a packet.", "src/telemetry/parser.py"),
        ("sink-client", "Sends batches downstream to the sink.", "src/core/sink.py"),
        ("retry-helper", "Retries a callable with a delay.", "src/util/retry.py"),
    ):
        (cards_dir / f"{cid}.json").write_text(json.dumps({
            "id": cid, "summary": summary, "keywords": summary.split()[:3],
            "files": [{"path": path, "why": "", "symbols": []}],
        }))


def _store(tmp_path, cards_dir):
    # provider=None: no LLM relevance filter, so the test pins the RETRIEVAL contract (scoring +
    # gate + render), not a model's judgment.
    return FileContextStore(str(cards_dir), repo_root=str(tmp_path), auto_bootstrap=False)


# --------------------------------------------------------------------------- the loop


def test_learned_reference_card_is_retrievable(tmp_path):
    """A card whose knowledge is REFERENCES (no summary, no pinned files) must clear the confidence
    gate. It used to score only on its items' ``why`` text, sink below the gate, and never reach a
    deep run: the run's hard-won paths were written down and then never handed to anyone."""
    proj = _project(tmp_path)
    cards = proj / ".cards"
    _decoy_cards(cards)
    _learned_card(cards)
    store = _store(proj, cards)

    assembled = store.assemble("where is clock-skew correction implemented in the relay")
    assert "learned-skew" in assembled.card_ids
    # Findable by the PATH it points at, too (nobody has to know the card's name).
    assert "learned-skew" in store.assemble("what is in config/relay.toml").card_ids


def test_reference_card_renders_into_a_deep_brief_naming_the_paths(tmp_path):
    """The deep worker's context_preamble must NAME each reference (the path), not just paste text:
    a body of code with no path is not a reference, it is a snippet the worker cannot act on."""
    proj = _project(tmp_path)
    cards = proj / ".cards"
    _decoy_cards(cards)
    _learned_card(cards)
    runner = _CapturingRunner()
    orch = _orch(_store(proj, cards), runner)

    orch._run_deep(
        PlanDecision(action="deep", goal="explain the clock-skew correction and its config",
                     deep_brief="explain it"),
        "explain the clock-skew correction and its config", "sonnet",
        ctx_meta={"user_id": "u1", "conv_id": "conv-A"},
    )

    preamble = runner.calls[0]["context_preamble"]
    assert "src/telemetry/correction.py" in preamble
    assert "config/relay.toml" in preamble
    # And the reference resolved LIVE: the worker gets the file's current contents, not a snapshot.
    assert "correct_clock_skew" in preamble
    assert "max_skew_ms" in preamble


def test_second_deep_run_from_a_different_conversation_gets_the_first_runs_references(tmp_path):
    """Cards are USER-scoped and conversation-independent. The whole point of the loop is that a
    LATER run, in a conversation that never saw the exploration, still starts with its findings."""
    proj = _project(tmp_path)
    cards = proj / ".cards"
    _decoy_cards(cards)
    store = _store(proj, cards)
    runner = _CapturingRunner()
    orch = _orch(store, runner)

    # Run 1 (conversation A) finishes and its future context is turned into reference items by the
    # card-update API, exactly as _apply_card_edits does.
    store.update_card(
        "learned-skew",
        fields={"name": "Relay clock-skew correction",
                "description": "Where clock-skew correction lives and what tunes it."},
        add=[
            {"type": "file", "locator": {"path": "src/telemetry/correction.py"}, "ts": 100.0,
             "why": "the clock-skew algorithm lives here"},
            {"type": "file", "locator": {"path": "config/relay.toml"}, "ts": 100.0,
             "why": "the [skew] tuning values"},
        ],
    )

    # Run 2: a DIFFERENT conversation, a related goal.
    orch._run_deep(
        PlanDecision(action="deep", goal="which config values does clock-skew correction depend on",
                     deep_brief="explain the config"),
        "which config values does clock-skew correction depend on", "sonnet",
        ctx_meta={"user_id": "u1", "conv_id": "conv-B-never-saw-run-1"},
    )

    preamble = runner.calls[0]["context_preamble"]
    assert "config/relay.toml" in preamble
    assert "src/telemetry/correction.py" in preamble


def test_unresolvable_reference_degrades_gracefully(tmp_path):
    """A path that moved or was deleted must render as a marked pointer, never an exception and
    never a silent drop: the next worker still learns the card once knew about it."""
    proj = _project(tmp_path)
    cards = proj / ".cards"
    _learned_card(cards, extra_items=[
        {"id": "f3", "type": "file", "locator": {"path": "src/telemetry/deleted.py"},
         "ts": 100.0, "why": "clock skew helper that used to live here"},
    ])
    store = _store(proj, cards)

    view = store.assemble("clock-skew correction helper").context_view
    assert "src/telemetry/deleted.py" in view
    assert "(unresolved)" in view
    # The resolvable siblings on the same card still resolve.
    assert "correct_clock_skew" in view


def test_render_block_lines_names_the_target():
    assert locator_label({"type": "file", "locator": {"path": "a/b.py"}}) == "a/b.py"
    assert locator_label({"type": "collection", "locator": {"name": "Dreams", "id": "c_1"}}) \
        == "Dreams (c_1)"
    assert locator_label({"type": "note", "locator": {"text": "x"}}) == ""

    lines = render_block_lines({"id": "i", "type": "file", "why": "the algorithm",
                                "locator": {"path": "a/b.py"}, "text": "code"})
    assert lines[0] == "  - (file) a/b.py -- the algorithm"
    # A note has no external target, so its header is unchanged.
    note = render_block_lines({"id": "n", "type": "note", "why": "a fact",
                               "locator": {"text": "t"}, "text": "t"})
    assert note[0] == "  - (note) a fact"


# --------------------------------------------------------------------------- per-source recency


def test_rendered_source_is_warmed_and_a_merely_held_source_is_not(tmp_path):
    """Per-source usage recency: the sources that actually RENDER into context are warmed; a source
    the card merely holds stays cold. Card-level usage_count could never make this distinction."""
    proj = _project(tmp_path)
    cards = proj / ".cards"
    # A card with more items than the render budget allows, so some items cannot be rendered.
    _learned_card(cards, extra_items=[
        {"id": f"cold{i}", "type": "note", "locator": {"text": f"unrelated fact {i}"},
         "ts": 1.0, "why": "unrelated"} for i in range(9)
    ])
    store = FileContextStore(str(cards), repo_root=str(proj), auto_bootstrap=False,
                             max_card_refs=2)

    store.assemble("clock-skew correction")

    import json
    card = json.loads((cards / "learned-skew.json").read_text())
    by_id = {it["id"]: it for it in card["content"]}
    assert by_id["f1"]["last_used_ts"] > 0.0      # rendered -> warmed
    assert by_id["f1"]["use_count"] == 1
    assert by_id["cold8"].get("last_used_ts", 0.0) == 0.0   # held but never rendered -> cold
    assert by_id["cold8"].get("use_count", 0) == 0


def test_a_hot_source_outranks_a_cold_one_under_the_render_budget():
    """Under a budget, recently-USED sources win. Cold ones rank lower, they are never dropped."""
    now = 10_000.0
    content = normalize_content([
        {"id": "hot", "type": "file", "locator": {"path": "a/hot.py"}, "ts": 1.0,
         "why": "relay skew", "last_used_ts": now},
        {"id": "cold", "type": "file", "locator": {"path": "a/cold.py"}, "ts": 1.0,
         "why": "relay skew", "last_used_ts": 0.0},
    ])
    ranked = rank_content_by_recency_relevance(content, tokenize("relay skew"), limit=1)
    assert [it["id"] for it in ranked] == ["hot"]
    # The cold one is only outranked, still reachable when the budget allows.
    both = rank_content_by_recency_relevance(content, tokenize("relay skew"), limit=2)
    assert {it["id"] for it in both} == {"hot", "cold"}


def test_legacy_items_without_usage_fields_rank_exactly_as_before():
    """Migration: a card written before per-source recency existed has no such fields. It must rank
    on relevance + learned-recency alone, with no rewrite and no behavior change."""
    content = normalize_content([
        {"id": "old", "type": "file", "locator": {"path": "a/old.py"}, "ts": 1.0, "why": "skew"},
        {"id": "new", "type": "file", "locator": {"path": "a/new.py"}, "ts": 500.0, "why": "skew"},
    ])
    assert all(it["last_used_ts"] == 0.0 and it["use_count"] == 0 for it in content)
    ranked = rank_content_by_recency_relevance(content, tokenize("skew"), limit=1)
    assert ranked[0]["id"] == "new"   # newest-learned wins the tie, exactly as before


def test_usage_recency_never_churns_the_rendered_bytes(tmp_path):
    """PROMPT-CACHE STABILITY: the recency data must not enter the rendered text, and re-warming
    every rendered source by the same amount must not reorder them. Two identical turns in a row
    must therefore render byte-identically."""
    proj = _project(tmp_path)
    cards = proj / ".cards"
    _learned_card(cards)
    store = FileContextStore(str(cards), repo_root=str(proj), auto_bootstrap=False)

    first = store.assemble("clock-skew correction and its config").context_view
    second = store.assemble("clock-skew correction and its config").context_view
    assert first == second
    assert "last_used_ts" not in first and "use_count" not in first


def test_mark_items_used_debounces_the_same_turn():
    """One turn assembles context several times (run-level, per goal, on a widening retry). They are
    the SAME use: without a debounce a turn would rewrite the card repeatedly and inflate the count."""
    content = normalize_content([
        {"id": "a", "type": "file", "locator": {"path": "a.py"}, "ts": 1.0, "why": "x"},
    ])
    assert mark_items_used(content, {"a"}, now=1000.0, min_interval=60.0) is True
    assert content[0]["use_count"] == 1
    # Same turn, moments later: no second stamp.
    assert mark_items_used(content, {"a"}, now=1010.0, min_interval=60.0) is False
    assert content[0]["use_count"] == 1
    assert content[0]["last_used_ts"] == 1000.0
    # A genuinely later turn stamps again.
    assert mark_items_used(content, {"a"}, now=2000.0, min_interval=60.0) is True
    assert content[0]["use_count"] == 2
