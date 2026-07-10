"""Full-horizon conversation recall — all offline, tmp_path session files only.

Covers the infinite-conversation invariants for the runner's local session store and the
executor's fallback conversation read:

  * I3 full horizon: an OLD session file (mtime far in the past) is still recalled when its
    content is relevant to the query; recency is a tie-break, never a filter.
  * I4 precision: a query that matches nothing pulls ONLY the small recency floor, never a
    prompt full of unrelated conversations; a matched query excludes non-matching, non-floor
    conversations.
  * I1 bounded: related_slices output respects max_chars, at most a hard-capped number of files
    are fully loaded per call, and per-file digests are cached by (mtime, size).
  * Index freshness: session files written AFTER construction become reachable.
  * Executor fallback: a task with conv_id but no ConversationStore reads the prior conversation
    BOUNDED (read_section max_bytes), and over-budget conversation reads keep the recent tail.
"""
import json
import os
import time

from quest_ai_runner.adapters import session_file_conversation_store as sfcs_module
from quest_ai_runner.adapters.claude_conversations_adapter import ClaudeConversationsAdapter
from quest_ai_runner.adapters.conversation_format import (
    rank_candidates_by_digest,
    truncate_transcript_middle,
)
from quest_ai_runner.adapters.session_file_conversation_store import SessionFileConversationStore
from quest_ai_runner.core.adapters import Observation
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator
from quest_ai_runner.runner.executor import CONV_CONTEXT_MAX_BYTES, TaskExecutor

from .conftest import StubProvider


# --- helpers ----------------------------------------------------------------

def _write_conv(sessions_dir, name, messages, *, age_days=None, **extra):
    conv = {"messages": messages}
    conv.update(extra)
    path = sessions_dir / f"{name}.json"
    path.write_text(json.dumps(conv))
    if age_days is not None:
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))
    return path


def _exchange(topic_user, topic_ai):
    return [{"role": "user", "text": topic_user}, {"role": "assistant", "text": topic_ai}]


# --- I3: old-but-relevant recall over the full horizon -----------------------

def test_old_but_relevant_conversation_is_recalled(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    # An OLD conversation (200 days) about a distinctive topic, plus newer unrelated chatter.
    _write_conv(sessions, "old_walrus", _exchange(
        "how do we configure the walrus ivory export pipeline",
        "the walrus export pipeline is configured in the ivory manifest",
    ), age_days=200)
    for i in range(4):
        _write_conv(sessions, f"recent_misc_{i}", _exchange(
            f"note {i} about office scheduling and snack rotation",
            f"acknowledged scheduling note {i}",
        ), age_days=i)

    store = SessionFileConversationStore(sessions_dir=str(sessions))
    ctx = store.related_slices("walrus ivory export pipeline", {}, max_convs=2)

    assert "walrus" in ctx.text.lower()
    assert ctx.scanned == 5  # the whole horizon was considered


def test_relevance_beats_recency_in_digest_ranking():
    digests = {
        "old_relevant": "START: walrus ivory export pipeline configuration",
        "new_irrelevant": "START: office snack rotation and coffee schedule",
    }
    ts = {"old_relevant": time.time() - 200 * 86400, "new_irrelevant": time.time()}
    ranked = rank_candidates_by_digest(
        list(digests.keys()),
        digest_of=lambda k: digests[k],
        timestamp_of=lambda k: ts[k],
        query="walrus ivory export",
        top_n=1,
    )
    assert ranked == ["old_relevant"]


# --- I4: precision — unmatched candidates stay out ---------------------------

def test_matched_query_excludes_irrelevant_non_floor_conversation(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_conv(sessions, "old_walrus", _exchange(
        "how does the walrus ivory export pipeline work",
        "via the ivory manifest",
    ), age_days=100)
    # Three newer irrelevant conversations: the two newest form the recency floor; the third
    # (ZEBRA) is neither relevant nor floor, so it must NOT appear.
    _write_conv(sessions, "irrelevant_zebra", _exchange(
        "ZEBRAMARKER completely unrelated gardening question about tulips",
        "tulips need well drained soil",
    ), age_days=3)
    _write_conv(sessions, "floor_a", _exchange("newest note about lunch", "ok"), age_days=1)
    _write_conv(sessions, "floor_b", _exchange("second newest note about parking", "ok"), age_days=2)

    store = SessionFileConversationStore(sessions_dir=str(sessions))
    ctx = store.related_slices("walrus ivory export pipeline", {}, max_convs=2)

    assert "walrus" in ctx.text.lower()
    assert "ZEBRAMARKER" not in ctx.text


def test_unmatched_query_returns_only_recency_floor(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for i in range(4):
        _write_conv(sessions, f"conv_{i}", _exchange(
            f"topic {i} about the billing ledger and refunds",
            f"answer {i} about proration",
        ), age_days=10 + i)
    _write_conv(sessions, "newest_a", _exchange("note about the quarterly offsite", "ok"),
                age_days=0)
    _write_conv(sessions, "newest_b", _exchange("note about the design review", "ok"),
                age_days=1)

    store = SessionFileConversationStore(sessions_dir=str(sessions))
    # A query whose terms overlap NO conversation: only the 2 most recent (the floor) render.
    ctx = store.related_slices("xylophone quantum bratwurst", {}, max_convs=3)

    assert "newest_a" in ctx.text and "newest_b" in ctx.text
    assert "billing" not in ctx.text.lower()
    assert ctx.scanned == 6


# --- I1: bounded output, bounded full loads, cached digests ------------------

def test_related_slices_respects_max_chars(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    long_text = " ".join(f"walrus pipeline detail sentence {i}." for i in range(80))
    for i in range(5):
        _write_conv(sessions, f"conv_{i}", _exchange(
            f"walrus pipeline question {i} " + long_text, long_text,
        ))
    store = SessionFileConversationStore(sessions_dir=str(sessions))
    ctx = store.related_slices("walrus pipeline", {}, max_convs=3, max_chars=1200)
    assert len(ctx.text) <= 1200


def test_full_loads_per_call_are_hard_capped(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for i in range(25):
        _write_conv(sessions, f"conv_{i}", _exchange(
            f"walrus pipeline question number {i}", f"walrus answer number {i}",
        ))
    store = SessionFileConversationStore(sessions_dir=str(sessions))
    store.related_slices("walrus pipeline", {}, max_convs=3)  # warm the digest cache

    calls = {"n": 0}
    original = SessionFileConversationStore._load_conv

    def counting_load(self, key):
        calls["n"] += 1
        return original(self, key)

    monkeypatch.setattr(SessionFileConversationStore, "_load_conv", counting_load)
    ctx = store.related_slices("walrus pipeline", {}, max_convs=3)
    assert ctx.text
    # Warm-cache call: stage 1 is stat-only (digests cached), stage 2 loads at most the
    # hard-capped shortlist, no matter that 25 conversations exist.
    assert calls["n"] <= sfcs_module._MAX_FULL_LOADS


def test_digest_cache_invalidated_when_file_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(sfcs_module, "_RESCAN_SECONDS", 0.0)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    path = _write_conv(sessions, "mutable", _exchange("first topic about tulip gardening", "ok"))
    _write_conv(sessions, "other", _exchange("note about parking permits", "ok"))

    store = SessionFileConversationStore(sessions_dir=str(sessions))
    ctx = store.related_slices("tulip gardening", {}, max_convs=1)
    assert "tulip" in ctx.text.lower()

    # Rewrite the file about a new topic and bump its mtime: the cached digest must refresh.
    path.write_text(json.dumps({"messages": _exchange(
        "now all about the walrus ivory export pipeline", "walrus manifest updated",
    )}))
    future = time.time() + 5
    os.utime(path, (future, future))

    ctx2 = store.related_slices("walrus ivory export", {}, max_convs=1)
    assert "walrus" in ctx2.text.lower()


def test_file_written_after_construction_is_reachable(tmp_path, monkeypatch):
    monkeypatch.setattr(sfcs_module, "_RESCAN_SECONDS", 0.0)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_conv(sessions, "existing", _exchange("note about parking permits", "ok"))
    store = SessionFileConversationStore(sessions_dir=str(sessions))

    _write_conv(sessions, "late_arrival", _exchange(
        "brand new conversation about the walrus ivory pipeline", "noted",
    ))
    ctx = store.related_slices("walrus ivory pipeline", {}, max_convs=2)
    assert "walrus" in ctx.text.lower()


def test_non_conversation_json_files_are_ignored(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "qar_state_like.json").write_text(json.dumps({"model": "x", "persona": "y"}))
    _write_conv(sessions, "real", _exchange("walrus pipeline question", "answer"))
    store = SessionFileConversationStore(sessions_dir=str(sessions))
    ctx = store.related_slices("walrus pipeline", {}, max_convs=2)
    assert ctx.scanned == 1
    assert "qar_state_like" not in ctx.text


# --- transcript truncation keeps the tail ------------------------------------

def test_truncate_transcript_middle_short_unchanged():
    assert truncate_transcript_middle("USER: hi\nASSISTANT: hello", 500) == \
        "USER: hi\nASSISTANT: hello"


def test_truncate_transcript_middle_keeps_head_and_tail():
    lines = [f"USER: message number {i}" for i in range(200)]
    lines.append("USER: FINALMARKER the very last message")
    text = "\n".join(lines)
    out = truncate_transcript_middle(text, 800)
    assert len(out) <= 800
    assert out.startswith("USER: message number 0")
    assert "FINALMARKER" in out
    assert "elided" in out


def test_conversations_adapter_read_section_keeps_recent_tail(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    messages = [{"role": "user", "text": f"turn number {i} of a very long conversation"}
                for i in range(300)]
    messages.append({"role": "user", "text": "TAILMARKER the newest turn"})
    _write_conv(sessions, "long_conv", messages)

    adapter = ClaudeConversationsAdapter(sessions_dir=str(sessions))
    obs = adapter.read_section("long_conv", max_bytes=1000)
    assert obs.kind == "read"
    assert len(obs.text) <= 1000
    assert "TAILMARKER" in obs.text


# --- executor fallback conv read is bounded -----------------------------------

class RecordingConvRetrieval:
    """RetrievalAdapter stub that records read_section kwargs and serves a conversation."""

    def __init__(self):
        self.read_calls = []

    def read_section(self, rel_path, *, start_line=None, end_line=None, heading=None,
                     max_bytes=None):
        self.read_calls.append({"rel_path": rel_path, "max_bytes": max_bytes})
        return Observation(kind="read", rel_path=rel_path, text="USER: prior chat turn")

    def grep(self, pattern, *, scope=None, max_hits=None):
        return Observation(kind="error", pattern=pattern, error="not found")

    def query(self, spec):
        return Observation(kind="error", error="query unsupported")


class MinimalClient:
    configured = True

    def __init__(self):
        self.reports = []

    def report_done(self, task_id, result):
        self.reports.append((task_id, "done", result))

    def report_needs_you(self, task_id, result, decision_id):
        self.reports.append((task_id, "needs_you", result))

    def report_failed(self, task_id, result):
        self.reports.append((task_id, "failed", result))


def test_executor_fallback_conv_read_passes_byte_cap():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    retrieval = RecordingConvRetrieval()
    orch = Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider))
    assert getattr(orch, "conversation_store", None) is None
    ex = TaskExecutor(MinimalClient(), orch)
    out = ex.execute({"id": "t1", "text": "summarize what we discussed", "conv_id": "conv_abc"})
    assert out.status == "done"
    conv_reads = [c for c in retrieval.read_calls if c["rel_path"] == "conv_abc"]
    assert conv_reads, "expected the fallback conversation read"
    assert conv_reads[0]["max_bytes"] == CONV_CONTEXT_MAX_BYTES
