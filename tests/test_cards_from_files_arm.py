"""QAR's arm of the hub's cards-from-files seat is its own topic bootstrap, and the seat resolves to it."""
from quest_ai_runner import hub
from quest_ai_runner.adapters.context_topic_cards import _llm_topic_cards
from quest_ai_runner.subconscious.qar_topic_bootstrap import QarTopicBootstrap
from tests.fake_topic_provider import TopicProvider, write_corpus


def test_the_seat_resolves_to_qars_arm_and_not_a_per_file_arm():
    assert hub.child().resolve("cards-from-files") is QarTopicBootstrap


def test_the_arm_proposes_what_the_bootstrap_pass_proposes(tmp_path):
    write_corpus(tmp_path)
    files = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.md"))
    expected = _llm_topic_cards(files, TopicProvider(), existing_cards=[], walk_root=tmp_path)
    got = QarTopicBootstrap(TopicProvider()).build(str(tmp_path), files, {})
    assert got == expected and len(got) >= 3
    assert all(len(card["files"]) > 1 for card in got), "a topic card groups files; the arm never returns a card per file"


def test_the_arm_without_a_model_proposes_nothing(tmp_path):
    write_corpus(tmp_path)
    assert QarTopicBootstrap(None).build(str(tmp_path), ["alpha/note0.md"], {}) == []


def test_changed_names_the_cards_whose_files_differ_from_their_digest(tmp_path):
    (tmp_path / "a.md").write_text("one", encoding="utf-8")
    cards = {"c": {"files": [{"path": "a.md", "sha256": "0" * 64}]}, "d": {"files": ["a.md"]}}
    assert QarTopicBootstrap(None).changed(str(tmp_path), cards) == ["c"]
