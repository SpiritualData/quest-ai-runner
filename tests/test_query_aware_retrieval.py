"""Query-aware retrieval routing (spec v3, work package C) — all offline, no network.

Covers:
  * C1 parsing: ``parse_goal_condition_reply`` splits the goal-condition-derivation reply into the
    restated instruction plus OPTIONAL structured constraints (time_range/topic_terms/actor/
    content_kind); malformed/absent/unrecognized constraints degrade to None (today's behavior).
  * C1 date wiring: ``_format_now_block`` / ``Orchestrator._derive_goal_condition`` thread a fixed
    injected "now" into the prompt, so relative-date resolution has a real "today" to work from.
  * C3 routing: when constraints are present, the orchestrator runs a bounded, hard-filtered
    cross-conversation search via the wired ConversationStore and folds it into context_view;
    absent constraints leave today's behavior untouched (no extra store call).
  * C2 filters: ``SessionFileConversationStore.related_slices`` and
    ``ClaudeConversationsAdapter.query`` apply a HARD time_range filter before relevance, and
    degrade to relevance-only (with an explicit note) when the filtered set is empty.
  * C2 card stores (item level): ``FileContextStore.assemble`` hard-filters card CONTENT ITEMS by
    ``ts`` when meta carries ``time_range`` (ts-less items always kept; an emptied card is dropped;
    a fully emptied selection degrades to unfiltered with an explicit note), the recent-context
    ``filter_relevant``/``render_recent_cards`` honor the same optional filter, and the
    orchestrator threads the parsed ``time_range`` into the assembly meta.
  * C4 planner tool round-trip: the planner can put time_range/topic_terms/actor/content_kind
    alongside a "query" read spec, and it reaches the RetrievalAdapter's query(spec) unchanged.
"""
import datetime
import json
import time
from typing import Any, Dict, List, Optional

from quest_ai_runner.adapters.claude_conversations_adapter import ClaudeConversationsAdapter
from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.adapters.session_file_conversation_store import SessionFileConversationStore
from quest_ai_runner.core.adapters import AssembledContext, ConversationContext, Observation
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    DECIDE_TOOL,
    Orchestrator,
    _format_now_block,
    parse_goal_condition_reply,
)
from quest_ai_runner.core.recent_context import filter_relevant, render_recent_cards

from .conftest import StubProvider, StubRetrieval


def _orch(provider, retrieval=None, **kw):
    return Orchestrator(retrieval=retrieval or StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), **kw)


def _write_conv(sessions_dir, name, messages, *, age_days=None):
    import os
    conv = {"messages": messages}
    path = sessions_dir / f"{name}.json"
    path.write_text(json.dumps(conv))
    if age_days is not None:
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))
    return path


def _exchange(user_text, ai_text):
    return [{"role": "user", "text": user_text}, {"role": "assistant", "text": ai_text}]


# --- C1: parse_goal_condition_reply (pure) -----------------------------------

def test_parse_goal_condition_reply_single_line_has_no_constraints():
    goal, constraints = parse_goal_condition_reply("Fix the pricing bug in checkout")
    assert goal == "Fix the pricing bug in checkout"
    assert constraints is None


def test_parse_goal_condition_reply_parses_full_constraints_line():
    raw = (
        "What tasks did the team finish last Wednesday\n"
        '{"time_range": {"start": "2026-07-08", "end": "2026-07-08"}, '
        '"topic_terms": ["greenhouse"], "actor": "team", "content_kind": "tasks_done"}'
    )
    goal, constraints = parse_goal_condition_reply(raw)
    assert goal == "What tasks did the team finish last Wednesday"
    assert constraints == {
        "time_range": {"start": "2026-07-08", "end": "2026-07-08"},
        "topic_terms": ["greenhouse"],
        "actor": "team",
        "content_kind": "tasks_done",
    }


def test_parse_goal_condition_reply_malformed_json_degrades_to_none():
    raw = "Do the thing\n{not valid json at all"
    goal, constraints = parse_goal_condition_reply(raw)
    assert goal == "Do the thing"
    assert constraints is None


def test_parse_goal_condition_reply_drops_unrecognized_keys_and_bad_enum_values():
    raw = (
        "Do the thing\n"
        '{"actor": "nonsense_value", "content_kind": "nonsense_kind", '
        '"made_up_field": "hallucinated", "topic_terms": ["ok"]}'
    )
    goal, constraints = parse_goal_condition_reply(raw)
    assert goal == "Do the thing"
    # Only the recognized, valid key survives; the bad enum values and unknown key are dropped.
    assert constraints == {"topic_terms": ["ok"]}


def test_parse_goal_condition_reply_all_constraints_invalid_yields_none():
    raw = 'Do the thing\n{"actor": "bogus", "made_up_field": 1}'
    goal, constraints = parse_goal_condition_reply(raw)
    assert goal == "Do the thing"
    assert constraints is None


def test_parse_goal_condition_reply_empty_input():
    assert parse_goal_condition_reply("") == ("", None)
    assert parse_goal_condition_reply("   ") == ("", None)


# --- C1: now/date wiring -------------------------------------------------------

def test_format_now_block_uses_the_given_fixed_now():
    block = _format_now_block("2026-07-09")
    assert block == "CURRENT DATE: 2026-07-09 (Thursday)"


def test_format_now_block_falls_back_to_system_clock_when_absent():
    block = _format_now_block(None)
    assert block.startswith("CURRENT DATE: ")


def test_derive_goal_condition_threads_fixed_now_into_the_prompt():
    class _Recording(StubProvider):
        def __init__(self):
            super().__init__(decisions=[])
            self.prompts = []

        def answer(self, messages, *, model, system=None):
            self.prompts.append("\n".join(m["content"] for m in messages))
            return "Show me tasks done last Wednesday"

    provider = _Recording()
    orch = _orch(provider)
    goal, constraints = orch._derive_goal_condition(
        "what did the team finish last Wednesday", now="2026-07-09")
    assert goal == "Show me tasks done last Wednesday"
    assert constraints is None  # the stub reply carried no second line
    assert "CURRENT DATE: 2026-07-09 (Thursday)" in provider.prompts[0]


def test_derive_goal_condition_returns_constraints_from_the_same_call():
    class _Recording(StubProvider):
        def __init__(self):
            super().__init__(decisions=[])

        def answer(self, messages, *, model, system=None):
            return (
                "What tasks did the team finish last Wednesday\n"
                '{"time_range": {"start": "2026-07-08", "end": "2026-07-08"}, '
                '"content_kind": "tasks_done", "actor": "team"}'
            )

    provider = _Recording()
    orch = _orch(provider)
    goal, constraints = orch._derive_goal_condition(
        "what did the team finish last Wednesday", now="2026-07-09")
    assert goal == "What tasks did the team finish last Wednesday"
    assert constraints == {
        "time_range": {"start": "2026-07-08", "end": "2026-07-08"},
        "content_kind": "tasks_done",
        "actor": "team",
    }


# --- C3: routing inside the orchestrator run() --------------------------------

class _FakeFilterableStore:
    """A ConversationStore stub that records related_slices calls (incl. ``filters``)."""

    def __init__(self, related_text="=== Related conversation: c9 ===\nUSER: greenhouse notes"):
        self._related_text = related_text
        self.related_calls = []

    def current_slice(self, conv_id, query, *, recent_turns=4, max_chars=6000, filters=None):
        return ConversationContext(scanned=0)

    def related_slices(self, query, scope, *, exclude_conv_id=None, max_convs=3, max_chars=6000,
                       filters=None):
        self.related_calls.append({
            "query": query, "scope": scope, "exclude_conv_id": exclude_conv_id, "filters": filters,
        })
        return ConversationContext(text=self._related_text, scanned=1)


class _ConstraintProvider(StubProvider):
    """answer() returns a goal-condition reply carrying constraints; plan()/answer() for the rest
    of the loop fall back to the StubProvider defaults (a plain "answer" decision)."""

    def __init__(self, constraint_reply, decisions=None):
        super().__init__(decisions=decisions or [{"action": "answer", "rationale": "ok",
                                                    "model_tier": "sonnet"}])
        self._constraint_reply = constraint_reply
        self._used = False

    def answer(self, messages, *, model, system=None):
        if not self._used:
            self._used = True
            return self._constraint_reply
        return super().answer(messages, model=model, system=system)


def test_run_routes_a_filtered_conversation_search_when_constraints_present():
    store = _FakeFilterableStore()
    provider = _ConstraintProvider(
        "What did the team finish last Wednesday\n"
        '{"time_range": {"start": "2026-07-08", "end": "2026-07-08"}, "content_kind": "tasks_done"}'
    )
    orch = _orch(provider, conversation_store=store)
    result = orch.run("what did the team finish last Wednesday", conv_id="conv_1",
                      conv_scope={"user_id": "u1"}, now="2026-07-09")

    assert result.retrieval_constraints == {
        "time_range": {"start": "2026-07-08", "end": "2026-07-08"},
        "content_kind": "tasks_done",
    }
    assert len(store.related_calls) == 1
    call = store.related_calls[0]
    assert call["filters"] == result.retrieval_constraints
    assert call["exclude_conv_id"] == "conv_1"


def test_run_does_not_search_conversations_when_no_constraints_detected():
    store = _FakeFilterableStore()
    provider = _ConstraintProvider("Fix the pricing bug in checkout")  # no second line
    orch = _orch(provider, conversation_store=store)
    result = orch.run("fix the pricing bug in checkout", conv_id="conv_1", now="2026-07-09")

    assert result.retrieval_constraints is None
    assert store.related_calls == []  # today's behavior: no extra store call


def test_run_degrades_cleanly_on_malformed_constraints_line():
    store = _FakeFilterableStore()
    provider = _ConstraintProvider("Fix the pricing bug\n{not json at all")
    orch = _orch(provider, conversation_store=store)
    result = orch.run("fix the pricing bug", conv_id="conv_1", now="2026-07-09")

    assert result.retrieval_constraints is None
    assert store.related_calls == []
    assert result.kind == "answer"  # the turn still completes normally


# --- C2: SessionFileConversationStore.related_slices time_range filter -------

def test_related_slices_time_range_hard_filters_before_relevance(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    # In-range: 2 days ago. Out-of-range: 10 days ago. Both share the same topic terms, so an
    # unfiltered relevance search would surface both; the time_range must exclude the old one.
    _write_conv(sessions, "in_range", _exchange(
        "greenhouse tomato notes for this week", "watered daily"), age_days=2)
    _write_conv(sessions, "out_of_range", _exchange(
        "greenhouse tomato notes from long ago", "watered daily too"), age_days=10)

    store = SessionFileConversationStore(sessions_dir=str(sessions))
    start = time.time() - 3 * 86400
    end = time.time() - 1 * 86400
    filters = {"time_range": {
        "start": time.strftime("%Y-%m-%d", time.localtime(start)),
        "end": time.strftime("%Y-%m-%d", time.localtime(end)),
    }}
    ctx = store.related_slices("greenhouse tomato notes", {}, max_convs=3, filters=filters)

    assert "in_range" in ctx.text
    assert "out_of_range" not in ctx.text
    assert ctx.degraded_note is None


def test_related_slices_time_range_degrades_when_nothing_matches(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_conv(sessions, "only_conv", _exchange(
        "greenhouse tomato irrigation schedule", "watered daily"), age_days=30)

    store = SessionFileConversationStore(sessions_dir=str(sessions))
    # A time window that excludes the only conversation on disk.
    filters = {"time_range": {"start": "2099-01-01", "end": "2099-01-02"}}
    ctx = store.related_slices("greenhouse tomato irrigation", {}, max_convs=3, filters=filters)

    # Degrades to relevance-only rather than a silent empty result.
    assert "greenhouse" in ctx.text.lower()
    assert ctx.degraded_note is not None
    assert "Note:" in ctx.text


def test_related_slices_topic_terms_are_folded_into_the_query(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_conv(sessions, "match", _exchange("board meeting agenda for budget review", "noted"))
    _write_conv(sessions, "nomatch", _exchange("unrelated lunch scheduling chat", "ok"), age_days=5)

    store = SessionFileConversationStore(sessions_dir=str(sessions))
    # An empty free-text query with topic_terms alone should still find the matching conversation.
    ctx = store.related_slices("", {}, max_convs=2, filters={"topic_terms": ["budget", "agenda"]})
    assert "board meeting" in ctx.text.lower()


# --- C2: ClaudeConversationsAdapter.query time_range filter -------------------

def _write_conv_with_timestamp(sessions_dir, name, messages, *, age_days):
    """ClaudeConversationsAdapter reads the conversation's OWN ``updated_at`` field for recency
    (unlike SessionFileConversationStore, it does not fall back to file mtime), so time_range
    filter tests need an explicit numeric timestamp in the conversation doc itself."""
    conv = {"messages": messages, "updated_at": time.time() - age_days * 86400}
    (sessions_dir / f"{name}.json").write_text(json.dumps(conv))


def test_claude_conversations_adapter_query_time_range_filters(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_conv_with_timestamp(sessions, "recent", _exchange("status update", "ok"), age_days=1)
    _write_conv_with_timestamp(sessions, "old", _exchange("status update too", "ok"), age_days=30)

    adapter = ClaudeConversationsAdapter(sessions_dir=str(sessions))
    start = time.time() - 2 * 86400
    obs = adapter.query({
        "time_range": {"start": time.strftime("%Y-%m-%d", time.localtime(start)), "end": None},
        "samples_per_cluster": 5,
    })
    assert obs.kind == "query"
    assert "recent" in obs.text
    assert "old" not in obs.text


def test_claude_conversations_adapter_query_time_range_degrades_when_empty(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_conv_with_timestamp(sessions, "only", _exchange("status update", "ok"), age_days=1)

    adapter = ClaudeConversationsAdapter(sessions_dir=str(sessions))
    obs = adapter.query({"time_range": {"start": "2099-01-01", "end": "2099-01-02"}})
    assert obs.kind == "query"
    assert "No conversations found in the specified time range" in obs.text
    assert "only" in obs.text


# --- C4: planner-visible filtered query round-trip ----------------------------

def test_decide_tool_schema_exposes_generic_filter_fields_on_reads():
    props = DECIDE_TOOL["input_schema"]["properties"]["reads"]["items"]["properties"]
    for key in ("time_range", "topic_terms", "actor", "content_kind"):
        assert key in props


class _RecordingQueryRetrieval(StubRetrieval):
    def __init__(self):
        super().__init__()
        self.query_specs = []

    def query(self, spec):
        self.query_specs.append(spec)
        return Observation(kind="query", text="[stub conversation search result]")


def test_planner_filtered_query_reaches_the_retrieval_adapter_unchanged():
    retrieval = _RecordingQueryRetrieval()
    read_spec = {
        "query": {"text": "greenhouse"},
        "time_range": {"start": "2026-07-08", "end": "2026-07-08"},
        "content_kind": "conversations",
    }
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [read_spec], "rationale": "search filtered history"},
        {"action": "answer", "rationale": "ok", "model_tier": "sonnet"},
    ])
    orch = Orchestrator(retrieval=retrieval, provider=provider, registry=ModelRegistry(provider))
    result = orch.run("what happened last Wednesday about the greenhouse")

    assert result.kind == "answer"
    assert len(retrieval.query_specs) == 1
    got = retrieval.query_specs[0]
    assert got["time_range"] == {"start": "2026-07-08", "end": "2026-07-08"}
    assert got["content_kind"] == "conversations"


# --- C2 card stores: item-level time_range filtering in FileContextStore ------

_RANGE = {"start": "2026-07-01", "end": "2026-07-02"}
_IN_TS = datetime.datetime(2026, 7, 1, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
_OUT_TS = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
_IN_ISO = "2026-07-01T12:00:00Z"
_OUT_ISO = "2026-05-01T12:00:00Z"


def _note(item_id, text, ts):
    return {"id": item_id, "type": "note", "locator": {"text": text}, "ts": ts, "why": ""}


def _write_card(cards_dir, card_id, *, keywords, content):
    card = {
        "id": card_id, "keywords": keywords, "summary": "", "files": [], "content": content,
        "conventions": [], "provenance": {}, "usage_count": 0, "last_outcome": "unknown",
    }
    (cards_dir / f"{card_id}.json").write_text(json.dumps(card), encoding="utf-8")


def _card_store(tmp_path):
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    return FileContextStore(str(cards_dir), confidence_threshold=0.0), cards_dir


def test_file_store_time_range_keeps_in_range_drops_out_of_range_items(tmp_path):
    store, cards_dir = _card_store(tmp_path)
    _write_card(cards_dir, "alpha-card", keywords=["alpha"], content=[
        _note("n_in", "inrange alpha detail", _IN_TS),
        _note("n_out", "outrange alpha detail", _OUT_TS),
    ])
    ac = store.assemble("alpha", meta={"time_range": _RANGE})
    assert "alpha-card" in ac.card_ids
    assert "inrange alpha detail" in ac.context_view
    assert "outrange alpha detail" not in ac.context_view
    # The structured item blocks (what the hybrid consolidator rebuilds from) are filtered too.
    (card_meta,) = [cm for cm in ac.card_metadata if cm["id"] == "alpha-card"]
    assert {b["id"] for b in card_meta["items"]} == {"n_in"}


def test_file_store_time_range_keeps_ts_less_items(tmp_path):
    store, cards_dir = _card_store(tmp_path)
    _write_card(cards_dir, "alpha-card", keywords=["alpha"], content=[
        _note("n_in", "inrange alpha detail", _IN_TS),
        _note("n_none", "undated alpha detail", 0.0),  # no real ts: must never be hidden
    ])
    ac = store.assemble("alpha", meta={"time_range": _RANGE})
    assert "undated alpha detail" in ac.context_view
    assert "inrange alpha detail" in ac.context_view


def test_file_store_card_emptied_by_time_filter_is_dropped(tmp_path):
    store, cards_dir = _card_store(tmp_path)
    _write_card(cards_dir, "stale-card", keywords=["alpha"], content=[
        _note("n1", "outrange alpha one", _OUT_TS),
        _note("n2", "outrange alpha two", _OUT_TS),
    ])
    _write_card(cards_dir, "fresh-card", keywords=["alpha"], content=[
        _note("n3", "inrange alpha three", _IN_TS),
    ])
    ac = store.assemble("alpha", meta={"time_range": _RANGE})
    assert "fresh-card" in ac.card_ids
    assert "stale-card" not in ac.card_ids
    assert "outrange" not in ac.context_view


def test_file_store_total_empty_time_filter_degrades_with_note(tmp_path):
    store, cards_dir = _card_store(tmp_path)
    _write_card(cards_dir, "only-card", keywords=["alpha"], content=[
        _note("n1", "outrange alpha detail", _OUT_TS),
    ])
    ac = store.assemble("alpha", meta={"time_range": _RANGE})
    # Never a silent empty: the unfiltered selection renders, led by an explicit note.
    assert "(Note:" in ac.context_view
    assert "time range" in ac.context_view
    assert "only-card" in ac.card_ids
    assert "outrange alpha detail" in ac.context_view


def test_file_store_without_time_range_is_unchanged(tmp_path):
    store, cards_dir = _card_store(tmp_path)
    _write_card(cards_dir, "alpha-card", keywords=["alpha"], content=[
        _note("n_out", "outrange alpha detail", _OUT_TS),
    ])
    # meta with no time_range key: no filtering, no note.
    ac = store.assemble("alpha", meta={"quest_id": "q1"})
    assert "outrange alpha detail" in ac.context_view
    assert "(Note:" not in ac.context_view


# --- C2 card stores: recent-context time_range filtering ----------------------

def test_filter_relevant_time_range_hard_filters_records():
    records = [
        {"id": "in", "turn_index": 5, "scope": "conv", "title": "billing invoice",
         "keywords": ["billing", "invoice"], "ts": _IN_ISO},
        {"id": "out", "turn_index": 5, "scope": "conv", "title": "billing invoice",
         "keywords": ["billing", "invoice"], "ts": _OUT_ISO},
    ]
    result = filter_relevant(records, "billing invoice question", is_followup=False,
                             max_cards=6, time_range=_RANGE)
    assert [r["id"] for r in result] == ["in"]


def test_filter_relevant_time_range_keeps_ts_less_records():
    records = [
        {"id": "in", "turn_index": 5, "scope": "conv", "title": "billing invoice",
         "keywords": ["billing", "invoice"], "ts": _IN_ISO},
        {"id": "undated", "turn_index": 5, "scope": "conv", "title": "billing invoice",
         "keywords": ["billing", "invoice"]},  # no ts at all: must never be hidden
    ]
    result = filter_relevant(records, "billing invoice question", is_followup=False,
                             max_cards=6, time_range=_RANGE)
    assert {r["id"] for r in result} == {"in", "undated"}


def test_filter_relevant_time_range_falls_back_when_everything_is_out_of_range():
    records = [
        {"id": "out", "turn_index": 5, "scope": "conv", "title": "billing invoice",
         "keywords": ["billing", "invoice"], "ts": _OUT_ISO},
    ]
    result = filter_relevant(records, "billing invoice question", is_followup=False,
                             max_cards=6, time_range=_RANGE)
    # Never a silent empty: the relevance-passing set survives unfiltered.
    assert [r["id"] for r in result] == ["out"]


def test_render_recent_cards_time_range_filters_items_and_drops_emptied_records():
    records = [
        {"id": "mixed", "title": "Mixed card", "items": [
            {"id": "i_in", "preview": "inrange item", "input_keywords": [],
             "last_used_ts": _IN_ISO},
            {"id": "i_out", "preview": "outrange item", "input_keywords": [],
             "last_used_ts": _OUT_ISO},
        ]},
        {"id": "stale", "title": "Stale card", "items": [
            {"id": "i_old", "preview": "old only item", "input_keywords": [],
             "last_used_ts": _OUT_ISO},
        ]},
    ]
    text, entries = render_recent_cards(records, "anything", time_range=_RANGE)
    assert "inrange item" in text
    assert "outrange item" not in text
    assert "old only item" not in text  # the emptied record is dropped whole
    assert {e["id"] for e in entries} == {"mixed"}
    assert "(Note:" not in text


def test_render_recent_cards_time_range_total_empty_degrades_with_note():
    records = [
        {"id": "stale", "title": "Stale card", "items": [
            {"id": "i_old", "preview": "old only item", "input_keywords": [],
             "last_used_ts": _OUT_ISO},
        ]},
    ]
    text, entries = render_recent_cards(records, "anything", time_range=_RANGE)
    # Never a silent empty: everything renders unfiltered with an explicit note.
    assert "(Note:" in text
    assert "time range" in text
    assert "old only item" in text
    assert {e["id"] for e in entries} == {"stale"}


def test_render_recent_cards_time_range_keeps_ts_less_items_and_records():
    records = [
        {"id": "undated-items", "title": "Undated items", "items": [
            {"id": "i1", "preview": "undated item", "input_keywords": [], "last_used_ts": ""},
        ]},
        {"id": "undated-record", "title": "Undated record", "preview": "whole card preview"},
    ]
    text, entries = render_recent_cards(records, "anything", time_range=_RANGE)
    assert "undated item" in text
    assert "whole card preview" in text
    assert "(Note:" not in text


# --- C2 card stores: the orchestrator threads time_range into assembly meta ---

class _MetaCapturingAssembler:
    """A ContextAssembler stub recording the ``meta`` each assemble() call received."""

    def __init__(self):
        self.metas: List[Optional[Dict[str, Any]]] = []

    def assemble(self, task_text, *, meta=None):
        self.metas.append(meta)
        return AssembledContext()

    def record(self, task_text, outcome):
        pass


def test_run_threads_time_range_into_assembly_meta():
    assembler = _MetaCapturingAssembler()
    provider = _ConstraintProvider(
        "What was done last Wednesday\n"
        '{"time_range": {"start": "2026-07-08", "end": "2026-07-08"}}'
    )
    orch = _orch(provider, context_assembler=assembler)
    result = orch.run("what was done last wednesday", now="2026-07-09")

    assert result.retrieval_constraints == {
        "time_range": {"start": "2026-07-08", "end": "2026-07-08"},
    }
    assert len(assembler.metas) == 1
    assert (assembler.metas[0] or {}).get("time_range") == {
        "start": "2026-07-08", "end": "2026-07-08",
    }


def test_run_without_constraints_leaves_meta_free_of_time_range():
    assembler = _MetaCapturingAssembler()
    provider = _ConstraintProvider("Fix the pricing bug in checkout")  # no constraints line
    orch = _orch(provider, context_assembler=assembler)
    orch.run("fix the pricing bug in checkout", now="2026-07-09")

    assert len(assembler.metas) == 1
    assert "time_range" not in (assembler.metas[0] or {})


def test_assemble_for_goal_threads_time_range_meta():
    assembler = _MetaCapturingAssembler()
    provider = StubProvider(decisions=[])
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), context_assembler=assembler)
    orch._assemble_for_goal_with_cards(
        "summarize last week", ctx_meta={"time_range": _RANGE, "quest_id": "q1"})

    assert len(assembler.metas) == 1
    assert (assembler.metas[0] or {}).get("time_range") == _RANGE
