"""User Input Understanding (Step 1) + SessionFileConversationStore — all offline.

Covers:
  * compact_message: long messages reduce to head + salient middle + tail within budget; short
    messages are unchanged.
  * SessionFileConversationStore.current_slice over a LONG synthetic conversation: the last USER
    turn is ALWAYS present (even at a tiny max_chars), a long AI turn is rendered compacted (not
    full), a relevant USER turn is preferred over a longer less-relevant AI turn, and AI turns are
    NOT auto-included by recency unless relevant.
  * SessionFileConversationStore.related_slices over multiple temp conversations.
  * Orchestrator Step-1 flow with a scripted provider + a fake ConversationStore:
      (a) an ambiguous "ok do it" gets a resolved goal_condition and emits EVENT_UNDERSTANDING,
      (b) a MORE_CONTEXT_NEEDED first reply triggers related_slices then resolves,
      (c) a CLARIFY: reply makes run() return a terminal confirm carrying the question and does
          NOT run the planner.
"""
import json

from quest_ai_runner.adapters.conversation_format import compact_message
from quest_ai_runner.adapters.session_file_conversation_store import SessionFileConversationStore
from quest_ai_runner.core.adapters import (
    EVENT_UNDERSTANDING,
    ConversationContext,
    Mode,
    ProgressEvent,
    StreamSink,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator

from .conftest import StubDeepRunner, StubProvider, StubRetrieval


# --- helpers ----------------------------------------------------------------

def _write_conv(sessions_dir, name, messages, **extra):
    conv = {"messages": messages}
    conv.update(extra)
    (sessions_dir / f"{name}.json").write_text(json.dumps(conv))


def _orch(provider, retrieval=None, **kw):
    return Orchestrator(retrieval=retrieval or StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), **kw)


class _RecordingSink:
    def __init__(self):
        self.events = []

    def update(self, event: ProgressEvent, mode: Mode) -> None:
        self.events.append(event)

    def types(self):
        return [e.type for e in self.events]


class _ScriptedProvider(StubProvider):
    """A provider whose answer() replays a scripted list of replies (for the resolve calls), then
    falls back to the StubProvider echo for the final grounded answer."""

    def __init__(self, *, answers, decisions):
        super().__init__(decisions=decisions)
        self._answers = list(answers)

    def answer(self, messages, *, model, system=None) -> str:
        if self._answers:
            return self._answers.pop(0)
        return super().answer(messages, model=model, system=system)


class _FakeStore:
    """A fake ConversationStore recording its calls and returning scripted slices."""

    def __init__(self, *, current_text="USER: earlier message\nASSISTANT: an earlier reply",
                 related_text="=== Related conversation: c2 ===\nUSER: the pricing doc"):
        self._current_text = current_text
        self._related_text = related_text
        self.current_calls = []
        self.related_calls = []

    def current_slice(self, conv_id, query, *, recent_turns=6, max_chars=6000):
        self.current_calls.append((conv_id, query))
        return ConversationContext(text=self._current_text, scanned=2)

    def related_slices(self, query, scope, *, exclude_conv_id=None, max_convs=3, max_chars=6000):
        self.related_calls.append((query, scope, exclude_conv_id))
        return ConversationContext(text=self._related_text, scanned=1)


# --- compact_message --------------------------------------------------------

def test_compact_message_short_is_unchanged():
    short = "Just a short note about pricing."
    assert compact_message(short, max_chars=400) == short


def test_compact_message_long_keeps_head_tail_and_salient_middle():
    # A long multi-sentence message: a distinctive head, a distinctive tail, and a varied middle.
    # The middle holds one sentence whose terms recur (the topic the message keeps returning to)
    # so TF-DF-IDF picks it as representative of the middle's topic.
    head = "First, here is the opening summary of the deployment plan."
    tail = "Finally, this is the closing note about the rollback procedure."
    topics = [
        "We considered the staging environment caching layer behaviour.",
        "Network latency between regions affects the replication window.",
        "The aardvark index rebuild must run before the aardvark cutover.",
        "Memory pressure on the workers grows during peak ingestion.",
        "Disk throughput on the primary node bounds the write path.",
    ]
    # Repeat the aardvark topic so its terms recur across the middle (high cluster_df), while the
    # other topics each appear once.
    middle = " ".join(topics + ["The aardvark cutover depends on the aardvark index being warm."])
    text = f"{head} {middle} {tail}"
    assert len(text) > 400

    out = compact_message(text, max_chars=400)
    assert len(out) <= 400
    # Head and tail are preserved (their distinctive opening/closing words survive).
    assert "opening summary" in out
    assert "rollback procedure" in out
    # The recurring salient middle topic is selected.
    assert "aardvark" in out
    # An elision marker shows content was dropped.
    assert "[...]" in out


def test_compact_message_long_returns_within_budget_even_without_sentences():
    # A long message with no sentence boundaries still compacts to head + tail within budget.
    text = "x" * 1000
    out = compact_message(text, max_chars=200)
    assert len(out) <= 200
    assert out.startswith("x")


# --- SessionFileConversationStore.current_slice -----------------------------

def test_current_slice_always_includes_last_user_turn_even_tiny_budget(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    messages = [
        {"role": "user", "text": "Tell me about kangaroo migration patterns in detail"},
    ]
    for i in range(8):
        messages.append({"role": "assistant", "text": f"filler unrelated weather note number {i}"})
        messages.append({"role": "user", "text": f"administrative remark {i}"})
    # The LAST user turn carries a unique anchor token.
    messages.append({"role": "user", "text": "ANCHORUSERTURN please proceed"})
    messages.append({"role": "assistant", "text": "an assistant acknowledgement"})
    _write_conv(sessions, "conv1", messages)

    store = SessionFileConversationStore(sessions_dir=str(sessions))
    # Even at an absurdly tiny budget the last USER turn is guaranteed present.
    ctx = store.current_slice("conv1", "kangaroo migration", recent_turns=4, max_chars=20)
    assert ctx.scanned == len(messages)
    assert "ANCHORUSERTURN" in ctx.text


def test_current_slice_compacts_long_ai_message(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    long_ai = " ".join(
        f"Sentence {i} about the kangaroo migration index and rebuild details." for i in range(60)
    )
    messages = [
        {"role": "user", "text": "Explain the kangaroo migration index design"},
        {"role": "assistant", "text": long_ai},
        {"role": "user", "text": "thanks, go on"},
    ]
    _write_conv(sessions, "conv1", messages)

    store = SessionFileConversationStore(sessions_dir=str(sessions))
    ctx = store.current_slice("conv1", "kangaroo migration index", recent_turns=4)
    # If the long AI turn is included at all, it must be COMPACTED, never rendered in full.
    assert long_ai not in ctx.text
    if "Sentence" in ctx.text:
        assert "[...]" in ctx.text


def test_current_slice_prefers_relevant_user_over_longer_irrelevant_ai(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    # A SHORT, highly relevant USER turn vs a LONG, irrelevant AI turn.
    relevant_user = "How do I configure the kangaroo migration index?"
    long_irrelevant_ai = " ".join(
        f"Unrelated weather digression number {i} about clouds and rainfall and humidity." for i in range(60)
    )
    messages = [
        {"role": "user", "text": relevant_user},          # relevant USER (older)
        {"role": "assistant", "text": long_irrelevant_ai},  # long but off-topic AI
        {"role": "user", "text": "ok continue"},            # anchor (last user turn)
    ]
    _write_conv(sessions, "conv1", messages)

    store = SessionFileConversationStore(sessions_dir=str(sessions))
    ctx = store.current_slice("conv1", "kangaroo migration index", recent_turns=4)
    # The relevant USER turn is preferred and present, verbatim.
    assert "kangaroo migration index" in ctx.text.lower()


def test_current_slice_ai_not_auto_included_by_recency(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    # Enough turns that relevance selection actually prunes. The query is about billing; the LAST AI
    # turn is recent but totally off-topic, so it must NOT be auto-included by recency. The last USER
    # turn (anchor) is the only guaranteed turn.
    messages = [
        {"role": "user", "text": "How does the billing pipeline charge monthly subscriptions?"},
        {"role": "assistant", "text": "The billing pipeline charges customers via the subscription billing service each month."},
        {"role": "user", "text": "And how does billing handle refunds and proration?"},
        {"role": "assistant", "text": "Billing computes proration on the subscription and issues refunds through the billing ledger."},
    ]
    for i in range(6):
        messages.append({"role": "user", "text": f"unrelated administrative remark {i} about scheduling"})
        messages.append({"role": "assistant", "text": f"acknowledged scheduling note {i}"})
    messages.append({"role": "user", "text": "ok do it"})  # anchor (last user turn)
    messages.append({"role": "assistant", "text": "ZEBRAFILLER completely unrelated note about office snacks and coffee"})
    _write_conv(sessions, "conv1", messages)

    store = SessionFileConversationStore(sessions_dir=str(sessions))
    ctx = store.current_slice("conv1", "billing pipeline subscriptions refunds", recent_turns=4)
    # The anchor (last user turn) is present.
    assert "ok do it" in ctx.text.lower()
    # The recent but irrelevant AI turn was not pulled in just because it is the latest message.
    assert "ZEBRAFILLER" not in ctx.text
    # The relevant billing turns surfaced.
    assert "billing" in ctx.text.lower()


def test_current_slice_missing_conversation_returns_empty(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    store = SessionFileConversationStore(sessions_dir=str(sessions))
    ctx = store.current_slice("nope", "anything")
    assert ctx.text == ""
    assert ctx.scanned == 0


# --- SessionFileConversationStore.related_slices ----------------------------

def test_related_slices_over_multiple_conversations(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_conv(sessions, "current", [
        {"role": "user", "text": "current chat about quarterly billing pipeline"},
        {"role": "assistant", "text": "ok"},
    ])
    _write_conv(sessions, "older_billing", [
        {"role": "user", "text": "how does the billing pipeline charge customers monthly"},
        {"role": "assistant", "text": "via the billing service"},
    ])
    _write_conv(sessions, "older_unrelated", [
        {"role": "user", "text": "what is the office coffee order policy"},
        {"role": "assistant", "text": "two pots a day"},
    ])

    store = SessionFileConversationStore(sessions_dir=str(sessions))
    ctx = store.related_slices("billing pipeline", {}, exclude_conv_id="current", max_convs=2)

    # The current conversation is excluded; only OTHER conversations are candidates.
    assert "current chat about quarterly" not in ctx.text
    assert ctx.scanned == 2
    # At least the billing-related conversation surfaces with a header.
    assert "older_billing" in ctx.text or "billing pipeline" in ctx.text.lower()


def test_related_slices_no_candidates_returns_empty(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_conv(sessions, "only", [{"role": "user", "text": "hi"}])
    store = SessionFileConversationStore(sessions_dir=str(sessions))
    ctx = store.related_slices("anything", {}, exclude_conv_id="only")
    assert ctx.text == ""
    assert ctx.scanned == 0


# --- Step 1 flow: ambiguous "ok do it" resolves + emits EVENT_UNDERSTANDING --

def test_step1_resolves_ambiguous_message_and_emits_understanding():
    store = _FakeStore()
    provider = _ScriptedProvider(
        answers=["Update the pricing docs to mention the new Pro tier."],  # the resolve reply
        decisions=[{"action": "answer", "rationale": "ok"}],
    )
    sink = _RecordingSink()
    orch = _orch(provider, conversation_store=store)
    res = orch.run("ok do it", conv_id="conv1", sink=sink, mode=Mode.LIVE)

    assert res.kind == "answer"
    # The store's current slice was pulled to resolve the request.
    assert store.current_calls and store.current_calls[0][0] == "conv1"
    # An EVENT_UNDERSTANDING fired carrying the resolved goal_condition.
    understanding = [e for e in sink.events if e.type == EVENT_UNDERSTANDING]
    assert understanding, "expected an EVENT_UNDERSTANDING"
    assert understanding[0].data.get("goal_condition") == \
        "Update the pricing docs to mention the new Pro tier."
    # The resolved request rode into the answer grounding (context_view).
    joined = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "UNDERSTOOD REQUEST" in joined
    assert "Update the pricing docs" in joined


def test_step1_more_context_needed_triggers_related_then_resolves():
    store = _FakeStore()
    provider = _ScriptedProvider(
        answers=[
            "MORE_CONTEXT_NEEDED",                               # first resolve reply
            "Edit the pricing doc to add the annual plan row.",  # after related_slices
        ],
        decisions=[{"action": "answer", "rationale": "ok"}],
    )
    sink = _RecordingSink()
    orch = _orch(provider, conversation_store=store)
    res = orch.run("the first one", conv_id="conv1", sink=sink,
                   conv_scope={"user_id": "u1"}, mode=Mode.LIVE)

    assert res.kind == "answer"
    # related_slices was consulted with the scope and the current conv excluded.
    assert store.related_calls, "expected related_slices to be called after MORE_CONTEXT_NEEDED"
    assert store.related_calls[0][2] == "conv1"
    # Resolved on the second pass.
    understanding = [e for e in sink.events if e.type == EVENT_UNDERSTANDING]
    assert understanding
    assert understanding[0].data.get("goal_condition") == \
        "Edit the pricing doc to add the annual plan row."


def test_step1_clarify_short_circuits_without_running_planner():
    store = _FakeStore()
    runner = StubDeepRunner(met=True)
    provider = _ScriptedProvider(
        answers=["CLARIFY: Which document did you mean?"],
        decisions=[{"action": "deep", "goal": "should not run", "rationale": "x"}],
    )
    sink = _RecordingSink()
    orch = _orch(provider, conversation_store=store, deep_runner=runner)
    res = orch.run("do that", conv_id="conv1", sink=sink, mode=Mode.LIVE)

    # Terminal confirm carrying the clarify question.
    assert res.kind == "confirm"
    assert res.question == "Which document did you mean?"
    # The planner loop did NOT run (no plan() calls) and no deep work executed.
    assert provider.plan_calls == 0
    assert runner.calls == []
    # No EVENT_UNDERSTANDING (we short-circuited to a decision instead).
    assert EVENT_UNDERSTANDING not in sink.types()


def test_self_contained_message_skips_step1_entirely():
    """A concrete, self-contained request must NOT hit the conversation store or the resolve LLM
    (zero added latency on the common path)."""
    store = _FakeStore()
    provider = _ScriptedProvider(
        answers=[],  # if Step 1 ran it would consume a scripted resolve answer; it must not.
        decisions=[{"action": "answer", "rationale": "ok"}],
    )
    sink = _RecordingSink()
    orch = _orch(provider, conversation_store=store)
    res = orch.run("Please update the onboarding guide to add the SSO setup section",
                   conv_id="conv1", sink=sink, mode=Mode.LIVE)

    assert res.kind == "answer"
    assert store.current_calls == []          # Step 1 never pulled context
    assert EVENT_UNDERSTANDING not in sink.types()


def test_no_conversation_store_means_no_step1():
    """With no store wired, Step 1 is inert even for an ambiguous message (back-compat)."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    sink = _RecordingSink()
    orch = _orch(provider)  # no conversation_store
    res = orch.run("ok do it", conv_id="conv1", sink=sink, mode=Mode.LIVE)
    assert res.kind == "answer"
    assert EVENT_UNDERSTANDING not in sink.types()


def test_needs_context_gate_is_conservative():
    provider = StubProvider(decisions=[])
    orch = _orch(provider)
    g = orch._needs_context_to_understand
    # Acknowledgements / short / anaphoric → needs context.
    assert g("ok do it") is True
    assert g("yes") is True
    assert g("the first one") is True
    assert g("do that") is True
    assert g("continue") is True
    # Self-contained requests with a concrete noun → does NOT need context.
    assert g("Please update the onboarding guide to add the SSO setup section") is False
    assert g("Fix the pricing calculation bug in the checkout flow") is False
