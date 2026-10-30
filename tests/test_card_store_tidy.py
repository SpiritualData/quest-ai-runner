"""The tidy through the repository port: one plan and one apply for a local folder, the Quest backend and Qdrant.

The backend here is ``BackendShapedRepository`` (evaluation/components/card_growth/backend_fake.py): in memory, no
quarantine, a server revision on every write, a conflicting writer on demand. Offline: tmp folders, no model.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from quest_ai_runner.adapters import card_store_tidy as tidy
from quest_ai_runner.adapters.card_repository import FilesystemCardRepository
from quest_ai_runner.adapters.file_context_store import FileContextStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evaluation" / "components"))
from card_growth.backend_fake import BackendShapedRepository  # noqa: E402

SD, PRODUCT = "sd-aaaaaa", "product-bbbbbb"


def load_tool():
    path = Path(__file__).resolve().parent.parent / "scripts" / "quarantine_cards.py"
    spec = importlib.util.spec_from_file_location("quarantine_cards_tool_for_tidy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def card(card_id: str, files: List[str] = (), **extra: Any) -> Dict[str, Any]:
    out = {"id": card_id, "name": card_id.split("--")[-1].replace("card:", "", 1), "summary": f"about {card_id}",
           "keywords": [card_id.split("--")[-1]], "files": [{"path": p} for p in files], "usage_count": 0,
           "provenance": {"created_by_task": "bootstrap"}}
    out.update(extra)
    return out


def inflated_cards() -> List[Dict[str, Any]]:
    """A store the ways real ones inflated: copies, a nested repo the review excludes, duplicates, a thin card,
    a used card, a card a person wrote."""
    return [
        card("auth", ["a/one.md", "a/two.md"], keywords=["auth", "login"]),
        card(f"{SD}--{PRODUCT}--auth", ["a/one.md", "a/two.md"], keywords=["auth", "sign-in"]),
        card("vendored-lib", ["vendor/lib/x.py", "vendor/lib/y.py"]),
        card("billing", ["b/one.md"], keywords=["billing"]),
        card("billing-again", ["b/one.md"], name="billing", keywords=["invoices"]),
        card("billing-for-sign-in", ["b/one.md"], keywords=["sign-in"]),   # another topic of the same file
        card("lockfile", ["c/package-lock.json"]),
        card("used", ["d/one.md"], usage_count=4),
        card("used-twin", ["d/one.md"], usage_count=2),
        card("by-hand", ["vendor/lib/x.py"], provenance={}),
    ]


def review(cards_dir: Path) -> None:
    cards_dir.mkdir(parents=True, exist_ok=True)
    (cards_dir / "folder_review.json").write_text(json.dumps(
        {"folders": {"vendor": {"index": False, "final": True, "reason": "a nested repository"}}}), encoding="utf-8")


def backend_with(cards: List[Dict[str, Any]]) -> BackendShapedRepository:
    repo = BackendShapedRepository()
    for c in cards:
        repo.seed(c["id"], c)
    return repo


def plain(cards: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    return {k: json.dumps(tidy.without_server_fields(v), sort_keys=True) for k, v in cards.items()}


def plan_for(repo, cards_dir: Path, reasons=tidy.DEFAULT_REASONS) -> tidy.StorePlan:
    return tidy.plan_store(repo.load_all(), cards_dir=cards_dir, reasons=reasons, repo=repo)


# --- one plan for every repository ---------------------------------------------------------------------

def test_the_backend_plan_equals_the_filesystem_plan_on_the_same_cards(tmp_path):
    folder = tmp_path / "folder"
    review(folder)
    for c in inflated_cards():
        (folder / f"{c['id']}.json").write_text(json.dumps(c), encoding="utf-8")
    tool = load_tool()
    script_entries, script_skipped = tool.plan(tool.load_cards(folder), reasons=set(tool.DEFAULT_REASONS),
                                               excluded=tool.excluded_prefixes_from_review(folder), threshold=0.8,
                                               distinct_topics=True)
    local = plan_for(FilesystemCardRepository(str(folder)), folder)
    lane = tmp_path / "lane"
    review(lane)
    backend = plan_for(backend_with(inflated_cards()), lane)

    def shape(entries):
        return sorted((e["reason"], e["key"], e["detail"], e.get("keeper", "")) for e in entries)
    assert shape(backend.entries) == shape(local.entries) == shape(script_entries)
    assert backend.skipped == local.skipped == script_skipped
    assert {e["reason"] for e in backend.entries} == {"compounded", "excluded", "surplus", "thin"}


def test_a_card_a_person_wrote_or_somebody_used_is_never_removed(tmp_path):
    review(tmp_path)
    repo = backend_with(inflated_cards())
    plan = plan_for(repo, tmp_path)
    run = tidy.RepositoryTidy(repo, tmp_path).apply(plan, repo.load_all())
    assert run.removed == len(plan.entries) and not run.failed and run.verified
    assert {"by-hand", "used", "used-twin", "auth", "billing", "billing-for-sign-in"} <= set(repo.cards)


# --- export first, keeper first, then removal ----------------------------------------------------------

def test_the_export_is_written_before_the_first_write_and_only_its_owner_reads_it(tmp_path):
    review(tmp_path)
    repo = backend_with(inflated_cards())
    plan = plan_for(repo, tmp_path)
    seen: List[Dict[str, Any]] = []

    def check(op, key):
        exports = list(tidy.state_dir_for(tmp_path).glob(f"*{tidy.EXPORT_SUFFIX}"))
        assert exports, f"{op} {key} came before the export"
        seen.append(json.loads(exports[0].read_text(encoding="utf-8")))
    repo.before_change = check
    run = tidy.RepositoryTidy(repo, tmp_path).apply(plan, repo.load_all())
    exported = seen[0]
    assert set(exported["cards"]) == {e["key"] for e in plan.entries}
    assert "auth" in exported["keepers"] and exported["store"]["repository"] == "BackendShapedRepository"
    assert stat.S_IMODE(os.stat(run.export).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(run.manifest).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tidy.state_dir_for(tmp_path)).st_mode) == 0o700
    copy = f"{SD}--{PRODUCT}--auth"
    assert repo.calls.index(("write", "auth")) < repo.calls.index(("delete", copy)), \
        "the keeper is updated before its copy is removed"


def test_keywords_of_a_removed_card_join_its_keeper_and_nothing_else_of_the_keeper_changes(tmp_path):
    review(tmp_path)
    repo = backend_with(inflated_cards())
    before = repo.read("auth")
    tidy.RepositoryTidy(repo, tmp_path).apply(plan_for(repo, tmp_path), repo.load_all())
    after = repo.read("auth")
    assert after["keywords"] == ["auth", "login", "sign-in"], "the keeper's own order first, then the new words"
    for name in ("name", "summary", "files", "usage_count"):
        assert after[name] == before[name]
    assert repo.read("billing")["keywords"] == ["billing", "invoices"]


def test_apply_then_undo_restores_every_card_exactly(tmp_path):
    review(tmp_path)
    repo = backend_with(inflated_cards())
    before = plain(repo.load_all())
    run = tidy.RepositoryTidy(repo, tmp_path).apply(plan_for(repo, tmp_path), repo.load_all())
    assert run.removed and plain(repo.load_all()) != before
    undone = tidy.RepositoryTidy(repo, tmp_path).undo(run.manifest)
    assert not undone.failed and undone.restored == run.removed and undone.reverted == run.updated
    assert plain(repo.load_all()) == before


def test_apply_then_undo_on_a_local_folder_puts_the_files_back(tmp_path):
    cards_dir = tmp_path / "cards"
    review(cards_dir)
    for c in inflated_cards():
        (cards_dir / f"{c['id']}.json").write_text(json.dumps(c), encoding="utf-8")
    repo = FilesystemCardRepository(str(cards_dir))
    before = plain(repo.load_all())
    run = tidy.RepositoryTidy(repo, cards_dir).apply(plan_for(repo, cards_dir), repo.load_all())
    assert run.removed and (cards_dir / "_quarantine" / "excluded" / "vendored-lib.json").is_file(), \
        "a local folder still MOVES a card aside"
    tidy.RepositoryTidy(repo, cards_dir).undo(run.manifest)
    assert plain(FilesystemCardRepository(str(cards_dir)).load_all()) == before


def test_a_card_changed_since_the_plan_is_skipped(tmp_path):
    review(tmp_path)
    repo = backend_with(inflated_cards())
    plan = plan_for(repo, tmp_path)
    cards = repo.load_all()
    repo.write("vendored-lib", dict(repo.read("vendored-lib"), summary="another writer changed this"))
    run = tidy.RepositoryTidy(repo, tmp_path).apply(plan, cards)
    assert "vendored-lib" in repo.cards
    assert run.skipped.get("the card changed since the plan") == 1
    assert run.removed == len(plan.entries) - 1


def test_a_second_run_finds_nothing(tmp_path):
    review(tmp_path)
    repo = backend_with(inflated_cards())
    tidy.RepositoryTidy(repo, tmp_path).apply(plan_for(repo, tmp_path), repo.load_all())
    calls = len(repo.calls)
    again = plan_for(repo, tmp_path)
    assert again.entries == []
    run = tidy.RepositoryTidy(repo, tmp_path).apply(again, repo.load_all())
    assert run.removed == 0 and len(repo.calls) == calls


def test_a_failure_mid_batch_stops_the_run_and_the_export_still_undoes_it(tmp_path):
    review(tmp_path)
    repo = backend_with(inflated_cards())
    before = plain(repo.load_all())
    deletes = []

    def fail(op, key):
        if op == "delete":
            deletes.append(key)
            return len(deletes) == 2
        return False
    repo.fail = fail
    plan = plan_for(repo, tmp_path)
    run = tidy.RepositoryTidy(repo, tmp_path).apply(plan, repo.load_all())
    assert run.failed and run.removed == 1 and len(deletes) == 2, "nothing is tried after the first failure"
    repo.fail = None
    undone = tidy.RepositoryTidy(repo, tmp_path).undo(run.manifest)
    assert not undone.failed and plain(repo.load_all()) == before


def test_an_undo_refuses_a_manifest_of_another_store(tmp_path):
    review(tmp_path)
    repo = backend_with(inflated_cards())
    run = tidy.RepositoryTidy(repo, tmp_path).apply(plan_for(repo, tmp_path), repo.load_all())
    other = backend_with([])
    other.base_url = "memory://another-backend"
    result = tidy.RepositoryTidy(other, tmp_path).undo(run.manifest)
    assert "another store" in result.failed and other.cards == {}


def test_a_stray_under_a_vector_point_id_joins_its_card_and_an_undo_says_it_cannot_write_that_id(tmp_path):
    repo = backend_with([card("topic", ["a/x.md"]),
                         card("card:topic", ["a/x.md"], content=[{"id": "n1", "type": "note", "ts": 1.0}],
                              summary="about topic", provenance={"created_by_task": "learn"})])
    plan = plan_for(repo, tmp_path, reasons=tidy.PROVABLE_REASONS)
    run = tidy.RepositoryTidy(repo, tmp_path).apply(plan, repo.load_all())
    assert set(repo.cards) == {"topic"} and repo.read("topic")["content"][0]["id"] == "n1"
    undone = tidy.RepositoryTidy(repo, tmp_path).undo(run.manifest)
    assert undone.skipped == {"its id is refused by every writer, so it stays in the export only": 1}
    assert "content" not in repo.read("topic"), "the keeper is restored as it was"


# --- when it runs on its own ---------------------------------------------------------------------------

def test_due_over_the_limit_or_on_ids_that_prove_copies():
    clean = {f"c{i}": card(f"c{i}") for i in range(3)}
    assert tidy.due(clean, 5) == ((), "")
    assert tidy.due(clean, 2)[0] == tidy.DEFAULT_REASONS
    assert tidy.due({**clean, f"{SD}--{PRODUCT}--x": card("x")}, 5)[0] == tidy.PROVABLE_REASONS


def backend_store(tmp_path, repo, folders: int = 20, **kwargs) -> FileContextStore:
    for number in range(folders):                       # a corpus with room for its topic cards
        (tmp_path / f"area{number}").mkdir(exist_ok=True)
        (tmp_path / f"area{number}" / "note.md").write_text("x", encoding="utf-8")
    store = FileContextStore(str(tmp_path / "cards"), repo_root=str(tmp_path), auto_bootstrap=False,
                             card_repository=repo, mount_mode="copy", self_heal=True, **kwargs)
    review(tmp_path / "cards")
    origin = store.origin_id()
    for key in list(repo.cards):
        repo.cards[key]["origin"] = origin
    return store


def test_a_backend_store_over_its_limit_plans_and_says_so_once_by_default(tmp_path):
    repo = backend_with(inflated_cards())
    store = backend_store(tmp_path, repo, max_cards=5)
    said: List[str] = []
    assert store.tidy_store(said.append) == 0
    assert len(repo.cards) == len(inflated_cards()), "a shared backend is never changed without the owner's yes"
    assert len(said) == 1 and "needs a tidy" in said[0] and "cards tidy --config" in said[0] and "--apply" in said[0]
    assert (tidy.state_dir_for(tmp_path / "cards") / tidy.PLAN_FILE).is_file()
    store.tidy_store(said.append)
    assert len(said) == 1, "the same plan is not announced again"


def test_a_backend_store_applies_when_the_lane_opts_in(tmp_path):
    repo = backend_with(inflated_cards())
    store = backend_store(tmp_path, repo, max_cards=5, tidy_mode="apply")
    said: List[str] = []
    removed = store.tidy_store(said.append)
    assert removed and "vendored-lib" not in repo.cards and "by-hand" in repo.cards
    assert len(said) == 1 and said[0].startswith("Tidied") and "cards tidy --undo" in said[0]


def test_a_backend_store_with_room_and_no_copies_is_left_alone(tmp_path):
    repo = backend_with(inflated_cards())
    store = backend_store(tmp_path, repo, max_cards=100, tidy_mode="apply")
    repo.cards.pop(f"{SD}--{PRODUCT}--auth")
    assert store.tidy_store() == 0 and len(repo.calls) == 0


def test_the_off_setting_turns_it_off(tmp_path):
    repo = backend_with(inflated_cards())
    store = backend_store(tmp_path, repo, max_cards=5, tidy_mode="off")
    said: List[str] = []
    assert store.tidy_store(said.append) == 0 and said == []


def test_a_local_folder_over_its_limit_applies_by_default(tmp_path):
    cards_dir = tmp_path / "cards"
    review(cards_dir)
    for c in inflated_cards():
        (cards_dir / f"{c['id']}.json").write_text(json.dumps(c), encoding="utf-8")
    store = FileContextStore(str(cards_dir), repo_root=str(tmp_path), auto_bootstrap=False, mount_mode="copy",
                             max_cards=5, self_heal=True)
    assert store.tidy_store() > 0
    assert (cards_dir / "_quarantine" / "excluded" / "vendored-lib.json").is_file()


def test_a_backend_store_at_its_limit_gives_the_notice(tmp_path):
    repo = backend_with(inflated_cards())
    store = backend_store(tmp_path, repo, max_cards=3)
    said: List[str] = []
    assert store.limit_guard.check_at_open(said.append) is True
    assert said and any("3" in line for line in said)


def test_the_setting_is_read_from_a_lane_file(tmp_path):
    from quest_ai_runner.config import RunnerConfig
    path = tmp_path / "qar.toml"
    path.write_text('card_tidy = "apply"\n', encoding="utf-8")
    assert RunnerConfig.from_file(str(path)).card_tidy == "apply"
    assert tidy.resolve_mode(None, BackendShapedRepository()) == "plan"
    assert tidy.resolve_mode(None, FilesystemCardRepository(str(tmp_path))) == "apply"
    assert tidy.resolve_mode("nonsense", BackendShapedRepository()) == "plan"


# --- by hand: the cards tidy command on a lane whose cards live on a backend ---------------------------

def test_the_command_tidies_a_backend_store_and_undoes_it(tmp_path, monkeypatch, capsys):
    import argparse
    from quest_ai_runner import cli_cards
    repo = backend_with(inflated_cards())
    cards_dir = tmp_path / "cards"
    backend_store(tmp_path, repo)                       # stamps the lane's origin on the cards
    before = plain(repo.load_all())
    monkeypatch.setattr(cli_cards, "lane_store", lambda args: (str(cards_dir), str(tmp_path), repo))

    def run(**kw):
        values = {"cards_op": "tidy", "apply": False, "undo": None, "config": None, "write": False, **kw}
        args = argparse.Namespace(**values)
        return cli_cards.run_cards_command(args, None)
    assert run() == 0 and plain(repo.load_all()) == before, "a dry run changes nothing"
    assert "would remove" in capsys.readouterr().out
    assert run(apply=True) == 0 and "vendored-lib" not in repo.cards
    manifest = tidy.latest_manifest(cards_dir)
    assert manifest is not None and str(manifest) in capsys.readouterr().out
    assert run(undo=str(manifest)) == 0
    assert plain(repo.load_all()) == before


# --- --merge-topics: the same files under another name ----------------------------------------------------
def run_tidy(cli_cards, **kw):
    import argparse
    values = {"cards_op": "tidy", "apply": False, "undo": None, "config": None, "write": False,
              "merge_topics": False, "cards_dir": None, "corpus": None, **kw}
    return cli_cards.run_cards_command(argparse.Namespace(**values), None)


def test_the_flag_shows_in_help_with_the_trade(capsys):
    from quest_ai_runner import cli_parser
    with pytest.raises(SystemExit):
        cli_parser._build_parser().parse_args(["cards", "tidy", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--merge-topics" in text and "less precise topics" in text and "--undo" in text


def test_merge_topics_joins_a_distinct_topic_onto_its_keeper_and_undo_restores_it(tmp_path, monkeypatch):
    from quest_ai_runner import cli_cards
    review(tmp_path)
    repo = backend_with(inflated_cards())
    backend_store(tmp_path, repo)
    cards_dir = tmp_path / "cards"
    monkeypatch.setattr(cli_cards, "lane_store", lambda args: (str(cards_dir), str(tmp_path), repo))
    before = plain(repo.load_all())
    keeper_before = repo.read("billing")
    assert run_tidy(cli_cards, apply=True) == 0
    assert "billing-for-sign-in" in repo.cards, "without the flag a distinct topic stays"
    assert run_tidy(cli_cards, undo=str(tidy.latest_manifest(cards_dir))) == 0
    assert plain(repo.load_all()) == before
    assert run_tidy(cli_cards, apply=True, merge_topics=True) == 0
    assert "billing-for-sign-in" not in repo.cards
    kept = repo.read("billing")
    assert "sign-in" in kept["keywords"] and "invoices" in kept["keywords"]
    for name in ("name", "summary", "files", "usage_count"):
        assert kept[name] == keeper_before[name]
    assert run_tidy(cli_cards, undo=str(tidy.latest_manifest(cards_dir))) == 0
    assert plain(repo.load_all()) == before


def test_merge_topics_on_a_local_folder_and_undo_restores_the_files_byte_for_byte(tmp_path, monkeypatch):
    from quest_ai_runner import cli_cards
    cards_dir = tmp_path / "cards"
    review(cards_dir)
    for c in inflated_cards():
        FilesystemCardRepository(str(cards_dir)).write(c["id"], c)      # the folder's own file format
    monkeypatch.setattr(cli_cards, "lane_store", lambda args: (str(cards_dir), str(tmp_path), None))
    snapshot = {p.name: p.read_bytes() for p in cards_dir.glob("*.json")}
    assert run_tidy(cli_cards, apply=True, merge_topics=True, cards_dir=str(cards_dir)) == 0
    assert not (cards_dir / "billing-for-sign-in.json").exists()
    assert "sign-in" in json.loads((cards_dir / "billing.json").read_text())["keywords"]
    assert (cards_dir / "vendored-lib.json").exists(), "only the topic merge runs on a local folder"
    assert run_tidy(cli_cards, undo=str(tidy.latest_manifest(cards_dir)), cards_dir=str(cards_dir)) == 0
    assert {p.name: p.read_bytes() for p in cards_dir.glob("*.json")} == snapshot


# --- a lane with only Quest credentials sees what the tidy does or would do -------------------------------------

def test_the_plan_notice_reaches_the_next_task_feed_with_the_command_and_the_opt_in(tmp_path):
    from quest_ai_runner.adapters import card_limit
    card_limit.reset_announcements()
    repo = backend_with(inflated_cards())
    store = backend_store(tmp_path, repo, max_cards=5)
    assert store.tidy_store() == 0 and len(repo.cards) == len(inflated_cards())
    notes = card_limit.take_task_notes()
    assert len(notes) == 1
    note = notes[0]
    assert note.startswith("About the context store, not this task: The context store needs a tidy because it holds "
                           "10 cards, over its limit of 5: removing 4 copies and cards about excluded folders")
    assert "quest-ai-runner cards tidy --config" in note and "--apply" in note
    assert 'card_tidy = "apply"' in note and "exported first" in note and "--undo" in note
    assert "second step" in note and "would bring the store to 5 cards" in note, "the merge step keeps it under 5"
    assert "--merge-topics" in note
    saved = json.loads((tidy.state_dir_for(tmp_path / "cards") / tidy.PLAN_FILE).read_text())
    assert saved["second_step"]["cards_after"] == 5
    assert [e["key"] for e in saved["second_step"]["entries"]] == ["billing-for-sign-in"]


def test_a_store_under_its_limit_after_the_first_step_gets_no_second_step(tmp_path):
    repo = backend_with(inflated_cards())
    store = backend_store(tmp_path, repo, max_cards=9)
    said: List[str] = []
    store.tidy_store(said.append)
    assert len(said) == 1 and "second step" not in said[0] and "--merge-topics" not in said[0]


def test_apply_takes_the_second_step_while_the_store_is_over_its_limit_and_undo_restores_both(tmp_path):
    repo = backend_with(inflated_cards())
    store = backend_store(tmp_path, repo, max_cards=5, tidy_mode="apply")
    before = plain(repo.load_all())
    said: List[str] = []
    assert store.tidy_store(said.append) == 5
    assert len(repo.cards) == 5 and "billing-for-sign-in" not in repo.cards
    assert "sign-in" in repo.read("billing")["keywords"]
    assert len(said) == 1 and "second step" in said[0] and "joined 1 card that pinned" in said[0]
    manifest = tidy.latest_manifest(tmp_path / "cards")
    assert said[0].count("cards tidy --undo") == 1 and str(manifest) in said[0], "one run, one manifest, one undo"
    removes = [json.loads(line) for line in manifest.read_text().splitlines() if '"op": "remove"' in line]
    assert [r["key"] for r in removes if r.get("step") == "merge-topics"] == ["billing-for-sign-in"]
    assert not tidy.RepositoryTidy(repo, tmp_path / "cards").undo(manifest).failed
    assert plain(repo.load_all()) == before


def test_the_limit_notice_of_a_backend_store_leads_with_the_tidy_and_says_why_learned_cards_come_in(tmp_path):
    from quest_ai_runner.adapters import card_limit
    card_limit.reset_announcements()
    repo = backend_with(inflated_cards())
    store = backend_store(tmp_path, repo, max_cards=3)
    assert store.limit_guard.check_at_open() is True
    note = card_limit.take_task_notes()[0]
    assert "To bring it under the limit run: quest-ai-runner cards tidy --config" in note
    assert note.index("cards tidy") < note.index("QAR_MAX_CARDS higher")
    assert "cannot be rebuilt from the files" in note


# --- the final check ------------------------------------------------------------------------------------------

class LateListing(BackendShapedRepository):
    """A backend whose listing shows a deleted card for a while (``stale`` listings), or never lets one go."""

    def __init__(self, stale: int = 0, keep: tuple = ()) -> None:
        super().__init__()
        self.stale, self.keep, self.ghosts = stale, set(keep), {}

    def delete(self, key: str) -> bool:
        if key in self.keep:
            self.calls.append(("delete", key))
            return True                                         # says yes, and the card stays
        self.ghosts[key] = self.cards.get(key)
        return super().delete(key)

    def load_all(self):
        out = super().load_all()
        if self.stale > 0:
            self.stale -= 1
            out.update({k: v for k, v in self.ghosts.items() if v is not None})
        return out


def test_a_delete_the_listing_shows_late_is_read_again_and_passes(tmp_path):
    review(tmp_path)
    repo = LateListing(stale=1)
    for c in inflated_cards():
        repo.seed(c["id"], c)
    plan = plan_for(repo, tmp_path)
    run = tidy.RepositoryTidy(repo, tmp_path, verify_wait=0.01).apply(plan, repo.load_all())
    assert run.removed == len(plan.entries) and run.verified and run.failed_checks == []


def test_a_failed_check_names_each_card_and_why_and_the_manifest_keeps_them(tmp_path, monkeypatch, capsys):
    review(tmp_path)
    repo = LateListing(keep=("vendored-lib",))
    for c in inflated_cards():
        repo.seed(c["id"], c)
    plan = plan_for(repo, tmp_path)
    run = tidy.RepositoryTidy(repo, tmp_path, verify_wait=0.01).apply(plan, repo.load_all())
    assert run.verified is False
    assert [(c["card"], c["check"]) for c in run.failed_checks] == [("vendored-lib", "removed")]
    assert "still holds it" in run.failed_checks[0]["why"] and "stays" in run.failed_checks[0]["why"]
    summary = json.loads(run.manifest.read_text().splitlines()[-1])["summary"]
    assert summary["verified"] is False and summary["failed_checks"] == 1
    assert summary["checks"] == run.failed_checks


def test_the_command_prints_how_many_checks_failed(tmp_path, monkeypatch, capsys):
    from quest_ai_runner import cli_cards
    repo = LateListing(keep=("vendored-lib",))
    for c in inflated_cards():
        repo.seed(c["id"], c)
    backend_store(tmp_path, repo)
    monkeypatch.setattr(tidy, "VERIFY_WAIT", 0.0)
    monkeypatch.setattr(cli_cards, "lane_store", lambda args: (str(tmp_path / "cards"), str(tmp_path), repo))
    run_tidy(cli_cards, apply=True)
    assert "checks failed   : 1" in capsys.readouterr().out


# --- manifests sort in the order the runs started -------------------------------------------------------------

def test_run_names_sort_in_start_order_within_one_second():
    stamps = [tidy.run_stamp() for _ in range(300)]
    assert sorted(stamps) == stamps and len(set(stamps)) == 300


def test_latest_manifest_is_the_last_run_even_in_the_same_second(tmp_path):
    review(tmp_path)
    repo = backend_with(inflated_cards())
    first = tidy.RepositoryTidy(repo, tmp_path).apply(plan_for(repo, tmp_path), repo.load_all())
    assert tidy.latest_manifest(tmp_path) == first.manifest
    tidy.RepositoryTidy(repo, tmp_path).undo(first.manifest)
    second = tidy.RepositoryTidy(repo, tmp_path).apply(plan_for(repo, tmp_path), repo.load_all())
    assert tidy.latest_manifest(tmp_path) == second.manifest != first.manifest


def test_a_compounded_card_that_is_the_only_copy_is_rekeyed_whole_and_undo_keeps_it(tmp_path):
    """The guard refuses every write to a compounded id, so a sole copy would stay stuck for ever: it moves to
    its one-hop id with everything it holds, used or not."""
    review(tmp_path)
    stuck = "root-a-111111--root-b-222222--root-a-111111--idealism"
    repo = backend_with([card(stuck, ["z/one.md"], usage_count=3)])
    plan = plan_for(repo, tmp_path)
    assert [(e["key"], e.get("becomes")) for e in plan.entries] == [(stuck, "root-a-111111--idealism")]
    run = tidy.RepositoryTidy(repo, tmp_path).apply(plan, repo.load_all())
    after = repo.load_all()
    assert set(after) == {"root-a-111111--idealism"} and after["root-a-111111--idealism"]["usage_count"] == 3
    # The old id is refused by every writer, so an undo cannot put it back and must not take the new card away.
    undone = tidy.RepositoryTidy(repo, tmp_path).undo(run.manifest)
    assert not undone.failed and set(repo.load_all()) == {"root-a-111111--idealism"}


def test_a_compounded_card_with_a_truncated_id_is_not_rekeyed(tmp_path):
    review(tmp_path)
    stuck = "root-a-111111--root-b-222222--root-a-111111--" + "x" * 150
    repo = backend_with([card(stuck, ["z/one.md"])])
    assert not plan_for(repo, tmp_path).entries


def test_a_store_with_more_topic_cards_than_folders_takes_the_second_step_without_being_over_its_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("QAR_BOOTSTRAP_MAX_CARDS_PER_FOLDER", "1")   # the rule under test: one card per folder
    # More topic cards than the few folders the corpus (and the store's own files inside it) can have.
    repo = backend_with([card(name, ["docs/a.md"], keywords=[name]) for name in
                         ("billing", "invoices", "sign-in", "reports", "audits", "payroll", "tax", "budgets")])
    store = backend_store(tmp_path, repo, folders=1, max_cards=1000, tidy_mode="apply")
    assert store.over_folder_budget(store._own_cards())
    assert store.tidy_store() == 7
    assert len(repo.load_all()) == 1


def test_a_local_folder_rekeys_its_compounded_sole_copies_and_joins_the_second_onto_the_first(tmp_path):
    """The folder tidy leaves compounded sole copies alone, and every write to such an id is refused."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for number in range(30):
        (corpus / f"area{number}").mkdir()
        (corpus / f"area{number}" / "note.md").write_text("x", encoding="utf-8")
    cards_dir = tmp_path / "cards"
    review(cards_dir)
    first = "root-a-111111--root-b-222222--root-a-111111--idealism"
    second = "root-a-111111--root-b-222222--root-b-222222--root-a-111111--idealism"
    repo = FilesystemCardRepository(str(cards_dir))
    for key in (first, second):       # on disk from before the guard: the repository refuses to write these ids now
        (cards_dir / f"{key}.json").write_text(json.dumps(card(key, ["area1/note.md"])), encoding="utf-8")
    store = FileContextStore(str(cards_dir), repo_root=str(corpus), auto_bootstrap=False, self_heal=True,
                             max_cards=1000)
    store.tidy_store()
    assert set(repo.load_all()) == {"root-a-111111--idealism"}


def test_a_second_compounded_sole_copy_with_a_distinct_topic_is_rekeyed_apart_and_a_retired_twin_is_removed(tmp_path):
    review(tmp_path)
    one = "root-a-111111--root-b-222222--root-a-111111--idealism"
    two = "root-a-111111--root-b-222222--root-b-222222--root-a-111111--idealism"
    three = "root-a-111111--root-b-222222--root-a-111111--folder"
    repo = backend_with([card(one, ["a/one.md"]), card(two, ["b/two.md"]), card(three, ["c/three.md"])])
    repo.quarantined = lambda key: object() if key == "root-a-111111--folder" else None
    plan = plan_for(repo, tmp_path)
    by_key = {e["key"]: e for e in plan.entries}
    assert by_key[one]["becomes"] == "root-a-111111--idealism"
    assert by_key[two]["becomes"].startswith("root-a-111111--idealism-") and by_key[two]["becomes"] != by_key[one]["becomes"]
    assert three in by_key and "becomes" not in by_key[three] and "keeper" not in by_key[three]
