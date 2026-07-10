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
  * C4 planner tool round-trip: the planner can put time_range/topic_terms/actor/content_kind
    alongside a "query" read spec, and it reaches the RetrievalAdapter's query(spec) unchanged.
"""
import json
import time

from quest_ai_runner.adapters.claude_conversations_adapter import ClaudeConversationsAdapter
from quest_ai_runner.adapters.session_file_conversation_store import SessionFileConversationStore
from quest_ai_runner.core.adapters import ConversationContext, Observation
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    DECIDE_TOOL,
    Orchestrator,
    _format_now_block,
    parse_goal_condition_reply,
)

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
