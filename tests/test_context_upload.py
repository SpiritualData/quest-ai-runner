"""``quest-ai-runner context upload``: a local store's cards (and existing vectors) to the Quest backend.

Offline against the fake card API. Proves: a dry run writes nothing; --apply sends the right batches
with the team or private scope; existing vectors are offered only when the store's embedder equals the
backend's (and the backend counts what it accepted); namespaced copy-imports are skipped; the local
embedder is never called; a re-run upserts the same ids.
"""
from pathlib import Path

import pytest

from quest_ai_runner import store_cli
from hubofhubs.recall import card_store_identity as ident
from quest_ai_runner.adapters.card_store_upload import REASON_IMPORTED, plan_upload
from quest_ai_runner.core.vector_scopes import CARD_SEED_SCOPE
from tests.quest_card_backend_fake import FakeCardBackend
from tests.test_card_store_mounts import VEC, CountingEmbedder, card, make_owner, seed
from tests.test_card_store_mounts import reader_store

pytest.importorskip("qdrant_client")
from quest_ai_runner.adapters.qdrant_vector_store import QdrantVectorStore  # noqa: E402

EMBEDDER = "test:embedder-a"


@pytest.fixture
def backend():
    fake = FakeCardBackend(embedder_id=EMBEDDER, dimension=VEC)
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def credentials(monkeypatch, backend):
    for name in ("QAR_QUEST_API_URL", "QAR_QUEST_API_KEY", "QAR_ENV_ID", "QAR_RUNNER_ENV_ID",
                 "QAR_EMBEDDER_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QUEST_BASE_URL", backend.url)
    monkeypatch.setenv("QUEST_API_KEY", "k")


def build_store(tmp_path, n=3, with_import=True):
    cards = [card(f"topic-{i}", [f"src/f{i}.py"]) for i in range(n)]
    if with_import:
        imported = card("other-store-topic", ["src/x.py"])
        imported["provenance"] = {"imported_from": "../org"}
        cards.append(imported)
    cards_dir = make_owner(tmp_path / "repo", cards)
    return cards_dir


def seeded_vectors(tmp_path, cards_dir):
    emb = CountingEmbedder()
    vs = QdrantVectorStore(path=str(tmp_path / "q"), embedder=emb, vector_size=VEC, embedder_id=EMBEDDER)
    seed(vs, reader_store(Path(cards_dir).parent))
    return vs, emb


def run_cli(*args):
    return store_cli.run(["context", "upload", *args])


def test_dry_run_prints_the_plan_and_writes_nothing(tmp_path, backend, capsys):
    cards_dir = build_store(tmp_path)
    code = run_cli("--cards-dir", str(cards_dir), "--team-id", "team_a")
    out = capsys.readouterr().out
    assert code == 0 and "dry run" in out
    assert "to upload:          3 in 1 batch" in out and REASON_IMPORTED in out
    assert not backend.bulk_batches and not backend.cards
    assert not [r for r in backend.requests if r[0] in ("PUT", "POST")]


def test_apply_uploads_to_the_team_in_batches_skipping_copy_imports(tmp_path, backend, capsys):
    cards_dir = build_store(tmp_path, n=150)
    code = run_cli("--cards-dir", str(cards_dir), "--team-id", "team_a", "--apply")
    assert code == 0 and "APPLIED" in capsys.readouterr().out
    assert [len(b["cards"]) for b in backend.bulk_batches] == [100, 50]
    assert all(b["team_id"] == "team_a" for b in backend.bulk_batches)
    assert len(backend.cards) == 150 and ("team_a", "other-store-topic") not in backend.cards
    sent = backend.bulk_batches[0]["cards"][0]["card"]
    assert "team_id" not in sent and sent["runner_env_id"]


def test_private_upload_sends_no_team(tmp_path, backend):
    cards_dir = build_store(tmp_path, n=2, with_import=False)
    assert run_cli("--cards-dir", str(cards_dir), "--private", "--apply") == 0
    assert all("team_id" not in b for b in backend.bulk_batches)
    assert set(backend.cards) == {("", "topic-0"), ("", "topic-1")}


def test_exactly_one_of_team_or_private_is_required(tmp_path):
    cards_dir = build_store(tmp_path)
    with pytest.raises(SystemExit):
        run_cli("--cards-dir", str(cards_dir))
    with pytest.raises(SystemExit):
        run_cli("--cards-dir", str(cards_dir), "--team-id", "t", "--private")


def test_vectors_are_sent_only_when_the_embedder_matches(tmp_path, backend, monkeypatch):
    cards_dir = build_store(tmp_path, n=3, with_import=False)
    vs, emb = seeded_vectors(tmp_path, cards_dir)
    embedded_before = emb.texts
    monkeypatch.setattr(store_cli, "open_vector_store", lambda *a, **k: vs)

    assert run_cli("--cards-dir", str(cards_dir), "--private", "--qdrant-path", "x",
                   "--embedder-id", EMBEDDER, "--apply") == 0
    sent = [i for b in backend.bulk_batches for i in b["cards"]]
    assert len(sent) == 3 and all(len(i["vector"]) == VEC for i in sent)
    assert backend.bulk_batches[0]["embedder_id"] == EMBEDDER and backend.bulk_batches[0]["dimension"] == VEC
    assert len(backend.vectors_seen) == 3

    backend.bulk_batches.clear()
    backend.vectors_seen.clear()
    assert run_cli("--cards-dir", str(cards_dir), "--private", "--qdrant-path", "x",
                   "--embedder-id", "other:model", "--apply") == 0
    assert all("vector" not in i for b in backend.bulk_batches for i in b["cards"])
    assert not backend.vectors_seen
    assert emb.texts == embedded_before          # nothing was ever embedded by the upload


def test_a_dimension_mismatch_withholds_the_vector(tmp_path, backend):
    cards_dir = build_store(tmp_path, n=2, with_import=False)
    vs, _ = seeded_vectors(tmp_path, cards_dir)
    plan = plan_upload(str(cards_dir), backend_embedder={"embedder_id": EMBEDDER, "dimension": VEC + 1},
                       vector_store=vs, local_embedder_id=EMBEDDER)
    assert plan.vectors_available == 2 and plan.vectors == {}
    assert "withheld" in plan.vector_note


def test_rerun_is_idempotent(tmp_path, backend):
    cards_dir = build_store(tmp_path, n=3, with_import=False)
    assert run_cli("--cards-dir", str(cards_dir), "--team-id", "team_a", "--apply") == 0
    first = dict(backend.cards)
    assert run_cli("--cards-dir", str(cards_dir), "--team-id", "team_a", "--apply") == 0
    assert set(backend.cards) == set(first)


def test_a_failed_batch_exits_nonzero_and_names_the_cards(tmp_path, backend, capsys):
    cards_dir = build_store(tmp_path, n=2, with_import=False)
    backend.fail_paths["/api/cards/bulk"] = (500, "bulk down")
    code = run_cli("--cards-dir", str(cards_dir), "--team-id", "team_a", "--apply")
    captured = capsys.readouterr()
    assert code == 1 and "FAILED" in captured.err and "topic-0" in captured.err
    assert "bulk down" in captured.out
    assert "qsk" not in captured.out + captured.err


def test_missing_credentials_is_a_clear_error(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("QUEST_BASE_URL")
    monkeypatch.delenv("QUEST_API_KEY")
    cards_dir = build_store(tmp_path)
    assert run_cli("--cards-dir", str(cards_dir), "--private") == 1
    assert "no Quest credentials" in capsys.readouterr().err


def test_copies_recording_their_origin_and_ids_that_may_never_be_stored_are_not_uploaded(tmp_path):
    """A copy that records its origin (not only ``imported_from``) is the source store's to upload, and a
    card under an id that repeats an import namespace never goes up (it would be refused there anyway)."""
    from quest_ai_runner.adapters.card_store_upload import REASON_REFUSED_ID
    copy = card("sd-aaaaaa--topic", ["src/c.py"])
    copy["provenance"] = {"origin_store": "st_other", "origin_id": "topic", "origin_root": "sd"}
    looped = card("sd-aaaaaa--root-bbbbbb--sd-aaaaaa--topic", ["src/l.py"])
    cards_dir = make_owner(tmp_path / "repo", [card("topic-0", ["src/f0.py"]), copy])
    (Path(cards_dir) / f"{looped['id']}.json").write_text(__import__("json").dumps(looped))
    plan = plan_upload(cards_dir)
    assert [cid for cid, _ in plan.uploads] == ["topic-0"]
    assert plan.skipped[REASON_IMPORTED] == ["sd-aaaaaa--topic"]
    assert plan.skipped[REASON_REFUSED_ID] == [looped["id"]]
