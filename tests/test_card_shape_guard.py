"""No card per file reaches the card store or the Quest backend: a batch of one card per file is refused with a notice, topic cards pass."""
import json

from quest_ai_runner.adapters import card_shape_guard
from quest_ai_runner.adapters.card_store_upload import apply_upload, plan_upload
from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.adapters.quest_api_card_repository import QuestApiCardRepository
from hubofhubs.recall import card_limit as limit_mod


def card(card_id, paths):
    return {"id": card_id, "name": card_id, "summary": card_id, "keywords": [], "files": [{"path": p} for p in paths]}


def per_file(count):
    return [card(f"file-{i}", [f"src/f{i}.py"]) for i in range(count)]


def topics(files, group):
    return [card(f"topic-{i}", [f"src/f{j}.py" for j in range(i, min(i + group, files))]) for i in range(0, files, group)]


def write_store(cards_dir, cards):
    cards_dir.mkdir(parents=True)
    for item in cards:
        (cards_dir / f"{item['id']}.json").write_text(json.dumps(item), encoding="utf-8")


def test_a_card_per_file_is_refused_with_the_breakdown():
    lines = card_shape_guard.refusal(per_file(80))
    assert len(lines) == 2 and "80 cards for 80 files" in lines[0] and "1.00 cards per file" in lines[0]


def test_topic_cards_pass_and_so_does_a_small_batch():
    assert card_shape_guard.refusal(topics(120, 6)) == []        # 0.17 cards per file
    assert card_shape_guard.refusal(per_file(10)) == []          # too few files to judge
    assert card_shape_guard.refusal([{"id": "chat", "files": []}] * 90) == []   # cards that pin no file are not judged


def test_a_limit_can_be_loosened_or_turned_off(monkeypatch):
    monkeypatch.setenv("QAR_MAX_CARDS_PER_FILE", "0")
    monkeypatch.setenv("QAR_MAX_SINGLE_FILE_CARD_SHARE", "0")
    assert card_shape_guard.refusal(per_file(80)) == []


def test_the_upload_plan_refuses_a_card_per_file_and_sends_nothing(tmp_path):
    write_store(tmp_path / "cards", per_file(80))
    plan = plan_upload(str(tmp_path / "cards"))
    assert plan.problems and "80 cards for 80 files" in plan.problems[0]

    class Repository:
        def bulk_write(self, *args, **kwargs):
            raise AssertionError("a refused plan must not reach the backend")

    result = apply_upload(plan, Repository())
    assert result["batches"] == 0 and len(result["failed"]) == 80 and result["errors"] == plan.problems


def test_the_upload_plan_passes_topic_cards(tmp_path):
    write_store(tmp_path / "cards", topics(120, 6))
    plan = plan_upload(str(tmp_path / "cards"))
    assert plan.problems == [] and len(plan.uploads) == 20


def test_the_backend_repository_refuses_a_card_per_file_before_any_request(monkeypatch):
    repository = QuestApiCardRepository(base_url="http://backend.invalid", api_key="k")
    monkeypatch.setattr(repository, "_call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request may be made")))
    reply = repository.bulk_write([(c["id"], c) for c in per_file(80)], team_id="")
    assert reply["batches"] == 0 and len(reply["failed"]) == 80 and "80 cards for 80 files" in reply["errors"][0]


def test_a_bootstrap_pass_of_a_card_per_file_writes_nothing_and_says_so(tmp_path, monkeypatch, caplog):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for i in range(60):
        (corpus / f"d{i}").mkdir()
        (corpus / f"d{i}" / "f.md").write_text(f"# note {i}\n", encoding="utf-8")
    from quest_ai_runner.adapters import context_card_builder
    monkeypatch.setattr(context_card_builder, "factory", lambda provider, model: type("Builder", (), {
        "build": staticmethod(lambda root, files, existing: [
            {"id": f"c{i}", "name": f"c{i}", "keywords": [], "summary": "s", "files": [f]} for i, f in enumerate(files)])})())
    limit_mod.take_task_notes()
    store = FileContextStore(cards_dir=str(tmp_path / "cards"), repo_root=str(corpus), auto_bootstrap=False)
    with caplog.at_level("WARNING"):
        written = store.bootstrap(root=str(corpus), provider=object())
    assert written == 0 and not list((tmp_path / "cards").glob("c*.json"))
    assert any("Refused 60 cards" in r.getMessage() for r in caplog.records)
    assert any("Refused 60 cards" in note for note in limit_mod.take_task_notes())
