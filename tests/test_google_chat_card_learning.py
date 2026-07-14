"""Google Chat content can be LEARNED as a card reference and RESOLVED fresh across turns.

This is the Google-Chat twin of ``test_conversation_card_learning.py``, proving the piece Joshua
asked for: Chat content is no longer "blocked on a resolver". ``GoogleChatAdapter`` now advertises
the RetrievalAdapter reference-resolution capability under its OWN type (``"chat_thread"``, distinct
from ``"conversation"``), so:

  * ``assemble()`` learns relevant threads onto the turn's ACTIVE card via the shared
    ``card_scoped_learning`` module (attach on first turn; bump-not-duplicate on the second; no-op
    with no active card),
  * a learned ``chat_thread`` reference RESOLVES back through ``build_resolver_registry`` by
    re-fetching FRESH through the adapter's own read path (never a stale snapshot).

All offline: the Chat API HTTP layer (``_get_json``) is stubbed with a mutable in-memory fake, no
network, no real token, no real space id.
"""
import time

from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.adapters.google_chat_adapter import GoogleChatAdapter, static_token_provider
from quest_ai_runner.adapters.card_scoped_learning import learn_card_references
from quest_ai_runner.adapters.reference_resolver import build_resolver_registry


class _FakeChat:
    """A mutable in-memory stand-in for the Google Chat REST API (no network).

    Holds one space with one thread whose latest message text can be CHANGED between reads, so a
    round-trip test can prove ``resolve_reference`` returns CURRENT content, not a snapshot.
    """

    SPACE = "spaces/AAA"
    THREAD = "spaces/AAA/threads/TVOICE"

    def __init__(self, text):
        self.text = text

    def get_json(self, url, token):
        # Messages listing for the space.
        if url.split("?")[0].endswith(f"{self.SPACE}/messages"):
            return {
                "messages": [
                    {
                        "text": self.text,
                        "createTime": "2026-07-10T12:00:00Z",
                        "thread": {"name": self.THREAD},
                        "sender": {"displayName": "Alex"},
                    }
                ]
            }
        # Spaces listing (only hit when space_names is not pinned).
        if url.split("?")[0].endswith("/spaces"):
            return {"spaces": [{"name": self.SPACE, "displayName": "Team"}]}
        return {}


def _store(tmp_path):
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    return FileContextStore(str(cards_dir), confidence_threshold=0.0, auto_bootstrap=False)


def _chat_refs(store, card_id):
    card = store.get_card(card_id) or {}
    return [
        it for it in (card.get("content") or [])
        if isinstance(it, dict) and it.get("type") == "chat_thread"
    ]


def _adapter(fake, store=None):
    a = GoogleChatAdapter(
        token_provider=static_token_provider("fake-token"),
        space_names=[_FakeChat.SPACE],
        group_by="thread",
        lookback_days=None,        # no time bound in the fake
        cache_ttl_seconds=0.0,     # force a fresh fetch on every read (prove "resolved fresh")
        card_store=store,
    )
    a._get_json = fake.get_json    # stub the only HTTP seam
    return a


# ---------------------------------------------------------------------------
# Round-trip: learn a chat_thread ref, resolve it back FRESH through the registry
# ---------------------------------------------------------------------------

def test_chat_thread_reference_resolves_fresh_through_registry(tmp_path):
    fake = _FakeChat("We moved voice STT to streaming and rebuilt the TTS audio pipeline")
    store = _store(tmp_path)
    store.update_card("voice", fields={
        "name": "Voice latency overhaul",
        "summary": "voice latency streaming tts audio pipeline",
    })
    adapter = _adapter(fake, store)

    # Attach the thread as a chat_thread reference via the shared learning module (adapter supplies
    # its own reference_type + make_locator, exactly as assemble() does internally).
    adapter._ensure_loaded()
    key = _FakeChat.THREAD
    locator = adapter.make_locator(key)
    assert locator["thread_or_message_id"] == key and locator["space"] == _FakeChat.SPACE
    learn_card_references(
        store, "voice", [key],
        ref_type=adapter.reference_type, locator_fn=adapter.make_locator,
        why="chat topic match", now=1000.0,
    )
    refs = _chat_refs(store, "voice")
    assert len(refs) == 1
    assert refs[0]["type"] == "chat_thread"

    # Resolve it back through the registry, wiring the adapter's OWN resolve_reference directly
    # (a bare callable -> coerced to a ReferenceResolver by build_resolver_registry).
    registry = build_resolver_registry(consumer_resolvers={"chat_thread": adapter.resolve_reference})
    resolver = registry["chat_thread"]
    rendered = resolver.resolve(locator, max_chars=4000)
    assert "streaming" in rendered and "TTS audio" in rendered

    # FRESH on use: change the live thread; a re-resolve reflects the NEW content (cache_ttl=0).
    fake.text = "Update: we also switched TTS to Deepgram for lower latency"
    rendered2 = resolver.resolve(locator, max_chars=4000)
    assert "Deepgram" in rendered2
    assert "streaming" not in rendered2

    # An unknown thread resolves to nothing (graceful), never an error.
    assert adapter.resolve_reference({"thread_or_message_id": "spaces/AAA/threads/GONE"}) is None


# ---------------------------------------------------------------------------
# assemble() learn wiring (mirrors test_conversation_card_learning.py)
# ---------------------------------------------------------------------------

def test_assemble_attaches_chat_thread_on_active_card(tmp_path):
    fake = _FakeChat("How did we cut the voice latency? streaming STT and a rebuilt TTS pipeline")
    store = _store(tmp_path)
    store.update_card("voice", fields={
        "name": "Voice latency overhaul",
        "summary": "voice latency streaming tts audio pipeline",
    })
    adapter = _adapter(fake, store)

    result = adapter.assemble("how did we fix the voice latency", meta={"thread_card_id": "voice"})
    # Surfaced in the context view ...
    assert "GOOGLE CHAT" in (result.context_view or "")
    # ... AND learned onto the card as a chat_thread reference.
    refs = _chat_refs(store, "voice")
    threads = {(r.get("locator") or {}).get("thread_or_message_id") for r in refs}
    assert _FakeChat.THREAD in threads, refs
    ref = next(r for r in refs if (r.get("locator") or {}).get("thread_or_message_id") == _FakeChat.THREAD)
    assert ref.get("why") == "chat topic match"
    assert ref.get("use_count", 0) >= 1
    assert float(ref.get("last_used_ts") or 0.0) > 0.0


def test_assemble_second_turn_bumps_without_duplicating(tmp_path, monkeypatch):
    fake = _FakeChat("How did we cut the voice latency? streaming STT and a rebuilt TTS pipeline")
    store = _store(tmp_path)
    store.update_card("voice", fields={
        "name": "Voice latency overhaul",
        "summary": "voice latency streaming tts audio pipeline",
    })
    adapter = _adapter(fake, store)

    import quest_ai_runner.adapters.google_chat_adapter as gc_mod
    clock = {"t": 1000.0}
    monkeypatch.setattr(gc_mod._time, "time", lambda: clock["t"])

    adapter.assemble("how did we fix the voice latency", meta={"thread_card_id": "voice"})
    refs = _chat_refs(store, "voice")
    assert len(refs) == 1
    first = refs[0]
    assert first.get("use_count") == 1
    assert float(first.get("last_used_ts")) == 1000.0

    clock["t"] = 1200.0
    adapter.assemble("remind me how the voice latency work went", meta={"thread_card_id": "voice"})
    refs2 = _chat_refs(store, "voice")
    assert len(refs2) == 1, refs2      # dedupe by locator: still one reference
    bumped = refs2[0]
    assert bumped.get("id") == first.get("id")
    assert bumped.get("use_count") == 2
    assert float(bumped.get("last_used_ts")) == 1200.0


def test_assemble_no_active_card_learns_nothing(tmp_path):
    fake = _FakeChat("How did we cut the voice latency? streaming STT and a rebuilt TTS pipeline")
    store = _store(tmp_path)
    store.update_card("voice", fields={
        "name": "Voice latency overhaul",
        "summary": "voice latency streaming tts audio pipeline",
    })
    with_store = _adapter(fake, store)
    without_store = _adapter(_FakeChat(fake.text), None)

    q = "how did we fix the voice latency"
    res_no_card = with_store.assemble(q, meta={})       # no thread_card_id
    res_baseline = without_store.assemble(q, meta=None)  # no store at all

    # Same context view, and nothing written to any card on the fallback path.
    assert "GOOGLE CHAT" in (res_baseline.context_view or "")
    assert (res_no_card.context_view or "") == (res_baseline.context_view or "")
    assert _chat_refs(store, "voice") == []
