"""The bootstrap obtains its proposed cards through the hub's cards-from-files seat, and the cards it writes are the ones it wrote before.

``fixtures/bootstrap_cards_before_seat.json`` holds the card files the bootstrap wrote for the corpus of ``fake_topic_provider`` and
that provider, recorded from the commit before the bootstrap called the seat. Fields that differ by run (times, origin and store
ids, the temporary folder, the order of a card's files and of equal-scoring terms, which follow a set) are normalised on both sides; everything else must
match exactly."""
import json
from pathlib import Path

from quest_ai_runner.adapters import context_card_builder
from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.subconscious.qar_topic_bootstrap import QarTopicBootstrap
from tests.fake_topic_provider import TopicProvider, write_corpus

VOLATILE = {"mtime", "created_at", "last_verified_at", "origin", "completed_at", "store_id", "first_bootstrap_completed_at", "index_hash"}
BEFORE = Path(__file__).parent / "fixtures" / "bootstrap_cards_before_seat.json"


def normalised(value, temporary=""):
    if isinstance(value, dict):
        out = {key: ("<run>" if key in VOLATILE else normalised(item, temporary)) for key, item in value.items()}
        if isinstance(out.get("tfdfidf_terms"), list):
            out["tfdfidf_terms"] = sorted(out["tfdfidf_terms"])    # equal-scoring terms come out in set order
        if isinstance(out.get("files"), list) and all(isinstance(f, dict) for f in out["files"]):
            out["files"] = sorted(out["files"], key=lambda entry: entry["path"])
        return out
    if isinstance(value, list):
        return [normalised(item, temporary) for item in value]
    return value.replace(temporary, "TMP") if isinstance(value, str) and temporary else value


def written_cards(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    write_corpus(corpus)
    store = FileContextStore(cards_dir=str(tmp_path / "cards"), repo_root=str(corpus), auto_bootstrap=False)
    written = store.bootstrap(root=str(corpus), provider=TopicProvider())
    cards = {f.name: json.loads(f.read_text(encoding="utf-8")) for f in sorted((tmp_path / "cards").glob("*.json"))}
    return written, normalised(cards, str(tmp_path))


def test_the_stored_cards_are_the_ones_the_bootstrap_wrote_before_it_called_the_seat(tmp_path):
    written, after = written_cards(tmp_path)
    before = normalised(json.loads(BEFORE.read_text(encoding="utf-8")))
    assert written == 3
    assert after == before


def test_the_bootstrap_asks_the_seat_for_its_proposal(tmp_path, monkeypatch):
    asked = []
    real = context_card_builder.factory

    def watching(provider, model):
        asked.append(type(real(provider, model)))
        return real(provider, model)

    monkeypatch.setattr(context_card_builder, "factory", watching)
    written_cards(tmp_path)
    assert asked == [QarTopicBootstrap]
