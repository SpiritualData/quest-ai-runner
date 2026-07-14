"""The adapter-agnostic card-scoped learning module (``card_scoped_learning``).

These cover the SHARED logic that ``ClaudeConversationsAdapter`` (and any future adapter) reuses,
proving it works for an ARBITRARY ``ref_type`` / ``locator_fn`` -- not just ``conversation`` /
``conv_id``:

  * the "union for surfacing, intersection for learning" term logic in isolation
    (``gate_terms`` / ``learnable_candidates``),
  * ``active_card_terms`` reading a card's topic terms generically,
  * ``learn_card_references`` attaching + re-stamping through a REAL ``FileContextStore`` with a
    NON-conversation reference type (a stand-in "second adapter" -- a Google-Chat-style thread ref),
  * dedupe + usage-bump on re-attach (no duplicate accrues; ``use_count`` / ``last_used_ts`` bump).

All offline: no network, no API key, no LLM (``auto_bootstrap=False``).
"""
from quest_ai_runner.adapters.card_scoped_learning import (
    active_card_terms,
    gate_terms,
    learn_card_references,
    learnable_candidates,
)
from quest_ai_runner.adapters.file_context_store import FileContextStore


def _store(tmp_path):
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    return FileContextStore(str(cards_dir), confidence_threshold=0.0, auto_bootstrap=False)


def _refs(store, card_id, ref_type):
    card = store.get_card(card_id) or {}
    return [
        it for it in (card.get("content") or [])
        if isinstance(it, dict) and it.get("type") == ref_type
    ]


# ---------------------------------------------------------------------------
# Pure term logic (no store)
# ---------------------------------------------------------------------------

def test_gate_terms_is_the_union_and_inert_without_a_card():
    query = {"voice", "latency"}
    card = {"streaming", "tts"}
    assert gate_terms(query, card) == {"voice", "latency", "streaming", "tts"}
    # No active card -> the gate is exactly the query terms (prior behaviour unchanged).
    assert gate_terms(query, set()) == query


def test_learnable_candidates_requires_both_query_and_card_overlap():
    # candidate -> its own terms
    terms = {
        "both": {"voice", "streaming"},      # overlaps query AND card -> eligible
        "query_only": {"voice", "unrelated"},  # overlaps query only -> NOT eligible
        "card_only": {"streaming", "misc"},    # overlaps card only -> NOT eligible
        "neither": {"weather"},                # overlaps nothing -> NOT eligible
    }
    query_terms = {"voice"}
    card_terms = {"streaming"}
    eligible = learnable_candidates(
        list(terms.keys()), lambda c: terms[c], query_terms, card_terms
    )
    assert eligible == ["both"]


def test_learnable_candidates_is_inert_when_no_card_terms():
    terms = {"a": {"voice"}, "b": {"latency"}}
    # An inactive card (empty card_terms) learns NOTHING.
    assert learnable_candidates(list(terms), lambda c: terms[c], {"voice", "latency"}, set()) == []


# ---------------------------------------------------------------------------
# active_card_terms (generic, any card)
# ---------------------------------------------------------------------------

def test_active_card_terms_reads_keywords_and_prose(tmp_path):
    store = _store(tmp_path)
    store.update_card("topic", fields={
        "name": "Voice latency overhaul",
        "summary": "streaming tts audio pipeline",
    })
    terms = active_card_terms(store, "topic")
    assert {"voice", "latency", "streaming", "tts"} <= terms


def test_active_card_terms_empty_without_store_or_card(tmp_path):
    store = _store(tmp_path)
    assert active_card_terms(None, "topic") == set()   # no store
    assert active_card_terms(store, None) == set()      # no card id
    assert active_card_terms(store, "missing") == set()  # unknown card


# ---------------------------------------------------------------------------
# learn_card_references with an ARBITRARY (non-conversation) ref_type
# ---------------------------------------------------------------------------

class _FakeChatAdapter:
    """A minimal stand-in "second adapter" that learns chat THREADS (not Claude conversations).

    Proves the shared module is not conversation-specific: it uses a different ``ref_type``
    (``"chat_thread"``) and a different locator shape (``{"space", "thread"}``), and still gets the
    union-gate, intersection-learn, attach, dedupe and usage-stamp for free. (In production this
    would additionally need a resolver for chat threads before it should persist references -- see
    the note in ``google_chat_adapter.assemble`` -- so this lives only in the test.)
    """

    REF_TYPE = "chat_thread"

    def __init__(self, card_store):
        self._card_store = card_store
        # thread id -> its digest terms
        self._threads = {
            "t-voice": {"voice", "latency", "streaming"},
            "t-pricing": {"pricing", "coupon"},
        }

    def learn(self, task_text_terms, card_id, *, now):
        card_terms = active_card_terms(self._card_store, card_id)
        surfacing = gate_terms(task_text_terms, card_terms)
        overlapping = [t for t, kw in self._threads.items() if kw & surfacing]
        eligible = learnable_candidates(
            overlapping, lambda t: self._threads[t], task_text_terms, card_terms
        )
        return learn_card_references(
            self._card_store,
            card_id,
            eligible,
            ref_type=self.REF_TYPE,
            locator_fn=lambda t: {"space": "spaces/AAA", "thread": t},
            why="chat topic match",
            now=now,
        )


def test_second_adapter_learns_arbitrary_ref_type(tmp_path):
    store = _store(tmp_path)
    store.update_card("voice", fields={
        "name": "Voice latency overhaul",
        "summary": "voice latency streaming tts audio pipeline",
    })
    adapter = _FakeChatAdapter(store)

    stamped = adapter.learn({"voice", "latency"}, "voice", now=1000.0)
    assert len(stamped) == 1

    refs = _refs(store, "voice", "chat_thread")
    assert len(refs) == 1
    ref = refs[0]
    assert ref["type"] == "chat_thread"                       # NOT "conversation"
    assert ref["locator"] == {"space": "spaces/AAA", "thread": "t-voice"}
    assert ref["why"] == "chat topic match"
    assert ref["use_count"] >= 1
    assert float(ref["last_used_ts"]) == 1000.0
    # Off-topic pricing thread was not learned (card not diluted).
    assert all(r["locator"]["thread"] != "t-pricing" for r in refs)


def test_reattach_dedupes_and_bumps_usage(tmp_path):
    store = _store(tmp_path)
    store.update_card("voice", fields={
        "name": "Voice latency overhaul",
        "summary": "voice latency streaming tts audio pipeline",
    })
    adapter = _FakeChatAdapter(store)

    adapter.learn({"voice", "latency"}, "voice", now=1000.0)
    first = _refs(store, "voice", "chat_thread")
    assert len(first) == 1
    assert first[0]["use_count"] == 1
    assert float(first[0]["last_used_ts"]) == 1000.0

    # Second turn, > the store's usage debounce later: same thread must NOT duplicate, only re-warm.
    adapter.learn({"remind", "voice"}, "voice", now=1200.0)
    second = _refs(store, "voice", "chat_thread")
    assert len(second) == 1, second
    assert second[0]["id"] == first[0]["id"]        # same item, not a new one
    assert second[0]["use_count"] == 2
    assert float(second[0]["last_used_ts"]) == 1200.0


def test_learn_is_a_noop_without_store_or_candidates(tmp_path):
    store = _store(tmp_path)
    # No store -> nothing stamped, no raise.
    assert learn_card_references(
        None, "voice", ["x"], ref_type="chat_thread",
        locator_fn=lambda t: {"thread": t}, why="", now=1.0,
    ) == []
    # No candidates -> nothing stamped, card untouched.
    assert learn_card_references(
        store, "voice", [], ref_type="chat_thread",
        locator_fn=lambda t: {"thread": t}, why="", now=1.0,
    ) == []
    assert (store.get_card("voice") or {}).get("content") in (None, [])
