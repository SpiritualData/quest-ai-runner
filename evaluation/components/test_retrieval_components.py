"""Offline tests for the retrieval, conversation_recall, recent_context and vector_recall components. No model call, no network.
Run: .venv/bin/python3 -m pytest evaluation/components/test_retrieval_components.py -q -p no:cacheprovider"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402

# impl -> cases that fail BY DESIGN (a keyword gather cannot bridge a paraphrase; a row-level index cannot see a body)
RETRIEVAL_GAPS = {
    "files_adapter": {"RET-019", "RET-020"},
    "cached_db": {"RET-019", "RET-020", "RET-021"},
    "composite": {"RET-019", "RET-020"},
    "mcp_server": {"RET-019", "RET-020"},
    "notion_pages": {"RET-004", "RET-010", "RET-019", "RET-020", "RET-021"},
    "drive_files": {"RET-019", "RET-020"},
    "chat_threads": {"RET-019", "RET-020"},
    "remote_files": {"RET-019", "RET-020"},
}


def run(component, impl):
    score = __import__(f"{component}.score", fromlist=["score_case"])
    fn = common.load_impl(impl, component)(common.CallMeter())
    rows = {c["id"]: score.score_case(fn, c) for c in common.load_cases(component)}
    return rows, score


@pytest.mark.parametrize("impl,gaps", sorted(RETRIEVAL_GAPS.items()))
def test_each_live_adapter_gathers_the_labelled_documents(impl, gaps):
    rows, _ = run("retrieval", impl)
    assert {i for i, r in rows.items() if not r["ok"]} == gaps


def test_retrieval_floor_is_far_below_every_adapter():
    rows, _ = run("retrieval", "first_three")
    assert sum(r["ok"] for r in rows.values()) <= 1


def test_conversation_recall_never_leaks_and_beats_the_floor():
    rows, _ = run("conversation_recall", "session_files")
    assert not any(r["leak"] for r in rows.values())
    assert {i for i, r in rows.items() if not r["ok"]} == {"CNV-010"}
    floor, _ = run("conversation_recall", "most_recent")
    assert sum(r["ok"] for r in floor.values()) < sum(r["ok"] for r in rows.values())


def test_changed_fixture_fails_loudly(tmp_path, monkeypatch):
    score = __import__("retrieval.score", fromlist=["score_case"])
    case = dict(common.load_cases("retrieval")[0], corpus_sha="00000000")
    with pytest.raises(RuntimeError, match="corpus.json changed"):
        score.score_case(lambda q, c: {"ids": []}, case)


def test_recent_context_gate_never_leaks_and_beats_the_floor():
    rows, _ = run("recent_context", "file_store_filter")
    assert not any(r["leak"] for r in rows.values())
    assert {i for i, r in rows.items() if not r["ok"]} == {"RCT-010"}
    floor, _ = run("recent_context", "last_turn")
    assert sum(r["leak"] for r in floor.values()) == 4


def test_vector_recall_qdrant_matches_the_in_memory_baseline():
    pytest.importorskip("qdrant_client")
    pytest.importorskip("fastembed")
    try:
        qdrant, _ = run("vector_recall", "qdrant_embedded")
    except RuntimeError as exc:  # the local embedder is not cached on this machine
        pytest.skip(str(exc))
    memory, _ = run("vector_recall", "memory_cosine")
    assert {i: r["ok"] for i, r in qdrant.items()} == {i: r["ok"] for i, r in memory.items()}
    assert sum(r["ok"] for r in qdrant.values()) >= 14
