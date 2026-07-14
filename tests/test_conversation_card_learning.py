"""Cross-session recall becomes a LEARNED card reference (usage-recency tracked).

``ClaudeConversationsAdapter.assemble`` used to recompute relevant past Claude sessions from the
WHOLE history every turn, disconnected from the card store: no persistence, no ``last_used_ts`` /
``use_count``. These tests cover the wiring that fixes that -- when the turn has an ACTIVE card
(``meta["thread_card_id"]``) and a ``card_store`` is wired:

  * a recall hit relevant to BOTH the request and the card's topic is attached to the card as a
    ``conversation`` reference (``update_card``), and
  * re-selecting the same conversation on a later turn BUMPS its ``last_used_ts`` / ``use_count``
    (``mark_sources_used``) instead of creating a duplicate reference,

while the NO-active-card path degrades to the exact prior global keyword + TF-DF-IDF scan.

All offline: no network, no API key, no LLM (cards are seeded via ``update_card`` fields, whose
prose supplies the card's topic terms; ``auto_bootstrap=False`` so no provider is needed).
"""
import json
import time

from quest_ai_runner.adapters.claude_conversations_adapter import ClaudeConversationsAdapter
from quest_ai_runner.adapters.file_context_store import FileContextStore


def _write_conv(sessions_dir, name, exchanges):
    """Write a minimal Claude session file (``<name>.json`` -> conv id ``<name>``)."""
    messages = []
    for user_text, ai_text in exchanges:
        messages.append({"role": "user", "text": user_text})
        messages.append({"role": "assistant", "text": ai_text})
    path = sessions_dir / f"{name}.json"
    path.write_text(json.dumps({"messages": messages}))
    return path


def _store(tmp_path):
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    return FileContextStore(str(cards_dir), confidence_threshold=0.0, auto_bootstrap=False)


def _conversation_refs(store, card_id):
    """The ``conversation``-type content items on ``card_id`` (fresh read)."""
    card = store.get_card(card_id) or {}
    return [
        it for it in (card.get("content") or [])
        if isinstance(it, dict) and it.get("type") == "conversation"
    ]


def test_recall_hit_attaches_as_conversation_reference(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    # On-topic for BOTH the query and the card (voice latency streaming).
    _write_conv(sessions, "voice-work", [
        ("How did we cut the voice latency", "We moved STT to streaming and rebuilt TTS audio"),
    ])
    # On-topic for a DIFFERENT idea (pricing) -- must not land on the voice card.
    _write_conv(sessions, "pricing-work", [
        ("Fix the pricing bug in checkout", "The discount coupon math was wrong"),
    ])

    store = _store(tmp_path)
    # Seed the active card. Its summary prose supplies the topic terms (voice/latency/streaming/tts).
    store.update_card("voice", fields={
        "name": "Voice latency overhaul",
        "summary": "voice latency streaming tts audio pipeline",
    })

    adapter = ClaudeConversationsAdapter(sessions_dir=str(sessions), card_store=store)
    result = adapter.assemble(
        "how did we fix the voice latency", meta={"thread_card_id": "voice"}
    )

    # The recall hit surfaced in the context view ...
    assert "voice-work" in (result.context_view or "")
    # ... AND was learned onto the card as a conversation reference.
    refs = _conversation_refs(store, "voice")
    conv_ids = {(r.get("locator") or {}).get("conv_id") for r in refs}
    assert "voice-work" in conv_ids, refs
    # The off-topic pricing conversation was NOT attached (card not diluted).
    assert "pricing-work" not in conv_ids
    voice_ref = next(r for r in refs if (r.get("locator") or {}).get("conv_id") == "voice-work")
    assert voice_ref.get("why") == "cross-session recall match"
    assert voice_ref.get("use_count", 0) >= 1
    assert float(voice_ref.get("last_used_ts") or 0.0) > 0.0


def test_second_turn_bumps_usage_without_duplicating(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_conv(sessions, "voice-work", [
        ("How did we cut the voice latency", "We moved STT to streaming and rebuilt TTS audio"),
    ])

    store = _store(tmp_path)
    store.update_card("voice", fields={
        "name": "Voice latency overhaul",
        "summary": "voice latency streaming tts audio pipeline",
    })
    adapter = ClaudeConversationsAdapter(sessions_dir=str(sessions), card_store=store)

    # Drive a controllable clock; the two turns are > the store's 60s usage debounce apart.
    import quest_ai_runner.adapters.claude_conversations_adapter as cca_mod
    clock = {"t": 1000.0}
    monkeypatch.setattr(cca_mod.time, "time", lambda: clock["t"])

    adapter.assemble("how did we fix the voice latency", meta={"thread_card_id": "voice"})
    refs = _conversation_refs(store, "voice")
    assert len(refs) == 1
    first = refs[0]
    assert first.get("use_count") == 1
    assert float(first.get("last_used_ts")) == 1000.0

    clock["t"] = 1200.0
    adapter.assemble("remind me how the voice latency work went", meta={"thread_card_id": "voice"})
    refs2 = _conversation_refs(store, "voice")
    # Still exactly ONE reference (dedupe by conv_id), but re-warmed.
    assert len(refs2) == 1, refs2
    bumped = refs2[0]
    assert bumped.get("id") == first.get("id")  # same item, not a new one
    assert bumped.get("use_count") == 2
    assert float(bumped.get("last_used_ts")) == 1200.0


def test_no_active_card_falls_back_to_global_scan(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_conv(sessions, "voice-work", [
        ("How did we cut the voice latency", "We moved STT to streaming and rebuilt TTS audio"),
    ])

    store = _store(tmp_path)
    store.update_card("voice", fields={
        "name": "Voice latency overhaul",
        "summary": "voice latency streaming tts audio pipeline",
    })

    query = "how did we fix the voice latency"
    # A store IS wired, but no card is active -> must behave exactly like the store-less adapter.
    with_store = ClaudeConversationsAdapter(sessions_dir=str(sessions), card_store=store)
    without_store = ClaudeConversationsAdapter(sessions_dir=str(sessions))

    result_no_card = with_store.assemble(query, meta={})           # no thread_card_id
    result_baseline = without_store.assemble(query, meta=None)     # no store at all

    assert (result_no_card.context_view or "") == (result_baseline.context_view or "")
    assert "voice-work" in (result_baseline.context_view or "")
    # Nothing was learned onto any card (the fallback path must not write).
    assert _conversation_refs(store, "voice") == []
