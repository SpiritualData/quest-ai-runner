"""The gates that keep a card store at hundreds of cards instead of tens of thousands.

Two real stores reached 8,557 and 32,070 cards for a corpus that should hold a few hundred. Each
test here pins one of the three reasons that happened, so none of them can come back quietly:

  * a deterministic folder exclusion that was not final, so an excluded subtree was re-opened and
    then outvoted by its own children,
  * cards covering the same files written again under different names,
  * no ceiling of any kind on how many cards a corpus may produce.

Everything is offline: no provider, no embedder, no network. Where a provider is needed it is a
fake that returns fixed JSON.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

from quest_ai_runner.adapters.card_repository import (
    QUARANTINE_DIR_NAME,
    FilesystemCardRepository,
)
from quest_ai_runner.adapters.file_context_store import (
    apply_card_ceilings,
    card_folder_key,
    card_is_too_thin,
    card_preference_key,
    collapse_cards_by_file_set,
    drop_verdicts_under_final_exclusions,
    final_exclusion_covering,
    path_is_data_file,
    verdict_is_final,
    verdict_is_pinned,
    _folder_review,
    _is_excluded_folder,
    effective_skip_dirs,
)


def make_card(card_id: str, files: List[str], **fields: Any) -> Dict[str, Any]:
    card: Dict[str, Any] = {
        "id": card_id,
        "name": fields.pop("name", card_id),
        "keywords": fields.pop("keywords", [card_id]),
        "summary": fields.pop("summary", card_id),
        "files": list(files),
        "usage_count": fields.pop("usage_count", 0),
        "provenance": fields.pop("provenance", {"created_by_task": "bootstrap"}),
    }
    card.update(fields)
    return card


def skip_verdict(**fields: Any) -> Dict[str, Any]:
    verdict = {"index": False, "mixed": False}
    verdict.update(fields)
    return verdict


# ---------------------------------------------------------------------------
# 1. A deterministic folder exclusion is FINAL
# ---------------------------------------------------------------------------
class TestFolderExclusionFinality:
    def test_a_deterministic_verdict_is_final_and_a_model_verdict_is_not(self):
        assert verdict_is_final(skip_verdict(source="nested_repo"))
        assert verdict_is_final(skip_verdict(source="duplicate"))
        assert verdict_is_final(skip_verdict(final=True))
        assert not verdict_is_final(skip_verdict(reason="looks generated"))
        assert not verdict_is_final(None)

    def test_an_explicit_final_false_in_the_file_wins(self):
        # The cache file has always been the surface a human edits to overturn a decision. A rule
        # that cannot be overturned there is a wall, not a rule.
        assert not verdict_is_final(skip_verdict(source="nested_repo", final=False))

    def test_a_pinned_verdict_is_recognised(self):
        assert verdict_is_pinned({"index": True, "pinned": True})
        assert not verdict_is_pinned({"index": True})
        assert not verdict_is_pinned(None)

    def test_the_final_exclusion_above_a_deep_descendant_is_found(self):
        verdicts = {
            "code": {"index": False, "mixed": True},
            "code/vendored": skip_verdict(source="nested_repo"),
        }
        # Checking the whole ancestor chain is what makes finality hold: the child of a child of
        # an excluded repository is still inside it.
        assert final_exclusion_covering(
            verdicts, "code/vendored/perspectives/philosophy") == "code/vendored"
        assert final_exclusion_covering(verdicts, "code/vendored") == "code/vendored"
        assert final_exclusion_covering(verdicts, "code/ours") is None

    def test_cached_verdicts_under_a_final_exclusion_are_dropped(self):
        # This is the cache invalidation for the bug: a store written by the buggy path holds
        # model verdicts for folders INSIDE a final exclusion, and keeping them re-opens the
        # subtree on every later pass.
        verdicts = {
            "code/vendored": skip_verdict(source="nested_repo"),
            "code/vendored/data": {"index": True, "mixed": False},
            "code/vendored/docs": {"index": True, "mixed": False},
            "code/ours": {"index": True, "mixed": False},
        }
        dropped = drop_verdicts_under_final_exclusions(verdicts)
        assert sorted(dropped) == ["code/vendored/data", "code/vendored/docs"]
        assert set(verdicts) == {"code/vendored", "code/ours"}

    def test_a_pinned_verdict_under_a_final_exclusion_survives(self):
        verdicts = {
            "code/vendored": skip_verdict(source="nested_repo"),
            "code/vendored/ours": {"index": True, "mixed": False, "pinned": True},
        }
        assert drop_verdicts_under_final_exclusions(verdicts) == []
        assert "code/vendored/ours" in verdicts

    def test_the_corpus_root_never_excludes_the_whole_tree(self):
        # "." is a prefix of every path composed as "./child". One cached root verdict collapsed a
        # real corpus walk from 76,593 files to 13.
        assert not _is_excluded_folder("anything/at/all", {"."})
        assert not _is_excluded_folder(".", {"."})
        assert _is_excluded_folder("vendor/pkg/a", {"vendor/pkg"})


class TestFolderReviewKeepsNestedRepoExcluded:
    """End to end over a real tmp corpus, with a fake provider that wants the subtree indexed."""

    def build_corpus(self, root: Path, nested_files: int) -> None:
        (root / "notes").mkdir(parents=True)
        for i in range(30):
            (root / "notes" / f"note{i}.md").write_text(f"# Note {i}\n\nProse.\n", encoding="utf-8")
        nested = root / "code" / "vendored"
        (nested / ".git").mkdir(parents=True)
        (nested / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        for area in ("alpha", "beta"):
            (nested / "perspectives" / area).mkdir(parents=True)
            for i in range(nested_files // 2):
                (nested / "perspectives" / area / f"gen{i}.md").write_text(
                    f"# Generated {area} {i}\n\nSummary.\n", encoding="utf-8")

    def keep_everything_provider(self):
        class KeepEverything:
            calls: List[str] = []

            def answer(self, messages, model=None, **kwargs):
                prompt = messages[0]["content"]
                self.calls.append(prompt)
                folders = []
                for line in prompt.splitlines():
                    if line.startswith('- "'):
                        folders.append(line.split('"')[1])
                return json.dumps([{"folder": f, "index": True, "mixed": False,
                                    "reason": "looks curated"} for f in folders])

        return KeepEverything()

    def test_the_nested_repository_stays_excluded_and_its_children_are_not_judged(self, tmp_path):
        corpus = tmp_path / "corpus"
        # Above _FOLDER_REVIEW_MIN_FILES (2000) so the review actually runs.
        self.build_corpus(corpus, nested_files=2100)
        cards_dir = tmp_path / "cards"
        provider = self.keep_everything_provider()

        excluded = _folder_review(corpus, effective_skip_dirs(corpus), provider, None, cards_dir)

        assert _is_excluded_folder("code/vendored", excluded), (
            "a nested repository must stay excluded however much the model likes its children"
        )
        assert _is_excluded_folder("code/vendored/perspectives/alpha", excluded)
        assert not _is_excluded_folder("notes", excluded)

        cached = json.loads((cards_dir / "folder_review.json").read_text(encoding="utf-8"))
        assert cached["version"] >= 2
        verdict = cached["folders"]["code/vendored"]
        assert verdict["index"] is False and verdict["final"] is True
        assert verdict["source"] == "nested_repo"
        # Nothing under it survives as a verdict, so no later pass can read a keep there.
        assert not [f for f in cached["folders"] if f.startswith("code/vendored/")]
        assert not any("code/vendored/perspectives" in call for call in provider.calls), (
            "children of a final exclusion must never be sent to the model at all"
        )

    def test_a_verdict_for_a_folder_nobody_asked_about_is_ignored(self, tmp_path):
        """The model answers a question; a key it was not asked for is not an answer.

        Found by review: a provider that answers its real batch honestly but volunteers one extra
        entry for a path inside a folder already excluded for good had that stray verdict merged
        and written into folder_review.json, even though nothing under a final exclusion is
        supposed to carry a verdict at all. It did not re-open the subtree (the parent's own
        verdict stays final, which short-circuits the superseded check) and it self-healed on the
        next load, but refusing the key is cheaper and more honest than cleaning up after it.
        """
        corpus = tmp_path / "corpus"
        self.build_corpus(corpus, nested_files=2100)
        cards_dir = tmp_path / "cards"

        class VolunteersExtra:
            def answer(self, messages, model=None, **kwargs):
                prompt = messages[0]["content"]
                asked = [line.split('"')[1] for line in prompt.splitlines()
                         if line.startswith('- "')]
                answers = [{"folder": f, "index": True, "mixed": False, "reason": "curated"}
                           for f in asked]
                answers.append({"folder": "code/vendored/perspectives/alpha", "index": True,
                                "mixed": False, "reason": "unsolicited"})
                answers.append({"folder": "a/folder/that/does/not/exist", "index": True,
                                "mixed": False, "reason": "unsolicited"})
                return json.dumps(answers)

        excluded = _folder_review(corpus, effective_skip_dirs(corpus), VolunteersExtra(), None,
                                  cards_dir)
        cached = json.loads((cards_dir / "folder_review.json").read_text(encoding="utf-8"))
        assert "code/vendored/perspectives/alpha" not in cached["folders"]
        assert "a/folder/that/does/not/exist" not in cached["folders"]
        assert _is_excluded_folder("code/vendored/perspectives/alpha", excluded)

    def test_a_non_mixed_model_skip_is_not_overturned_by_a_kept_child(self, tmp_path):
        # Re-opening a large SKIP on size alone is how the 23k-file subtree got back in. Only a
        # skip the model itself called "mixed" is provisional.
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        (cards_dir / "folder_review.json").write_text(json.dumps({"version": 2, "folders": {
            "state": skip_verdict(reason="tool state files"),
            "state/readme": {"index": True, "mixed": False},
            "workspace": skip_verdict(mixed=True),
            "workspace/notes": {"index": True, "mixed": False},
        }}), encoding="utf-8")
        corpus = tmp_path / "corpus"
        (corpus / "x").mkdir(parents=True)
        (corpus / "x" / "a.md").write_text("# A\n", encoding="utf-8")

        # No provider: the cached verdicts plus the deterministic safeguards decide everything,
        # and the corpus is too small for the model arm anyway.
        excluded = _folder_review(corpus, effective_skip_dirs(corpus), None, None, cards_dir)
        assert excluded == set(), "a corpus under the review's minimum size prunes nothing"

        # The rule itself, read off the same verdicts.
        verdicts = json.loads(
            (cards_dir / "folder_review.json").read_text(encoding="utf-8"))["folders"]
        assert not verdicts["state"]["mixed"]
        assert verdicts["workspace"]["mixed"]


# ---------------------------------------------------------------------------
# 2. Cards covering the same FILE SET are one card
# ---------------------------------------------------------------------------
class TestFileSetDedup:
    def test_an_identical_path_set_collapses_to_one_card(self):
        a = make_card("a", ["docs/x.md", "docs/y.md"], keywords=["x"])
        b = make_card("b", ["docs/y.md", "docs/x.md"], keywords=["y"])
        out = collapse_cards_by_file_set([a, b], [])
        assert [c["id"] for c in out] == ["a"]
        assert set(out[0]["keywords"]) == {"x", "y"}
        assert set(out[0]["files"]) == {"docs/x.md", "docs/y.md"}

    def test_a_high_overlap_collapses_and_a_low_one_does_not(self):
        base = make_card("base", [f"docs/f{i}.md" for i in range(10)])
        near = make_card("near", [f"docs/f{i}.md" for i in range(1, 11)])   # 9/11 ~= 0.82
        far = make_card("far", [f"docs/f{i}.md" for i in range(5, 15)])     # 5/15 ~= 0.33
        assert [c["id"] for c in collapse_cards_by_file_set([base, near], [])] == ["base"]
        assert sorted(c["id"] for c in collapse_cards_by_file_set([base, far], [])) == \
            ["base", "far"]

    def test_a_new_card_folds_into_an_existing_one_instead_of_adding_an_id(self):
        existing = make_card("stored", [{"path": "docs/x.md"}, {"path": "docs/y.md"}])
        existing["files"] = [{"path": "docs/x.md"}, {"path": "docs/y.md"}]
        fresh = make_card("fresh-restatement", ["docs/x.md", "docs/y.md"])
        out = collapse_cards_by_file_set([fresh], [existing])
        assert [c["id"] for c in out] == ["stored"], (
            "a restatement must update the card already in the store, never add a second copy"
        )

    def test_the_most_used_card_wins_and_creation_time_breaks_the_tie(self):
        used = make_card("used", ["docs/x.md", "docs/y.md"], usage_count=3)
        unused = make_card("unused", ["docs/x.md", "docs/y.md"], usage_count=0)
        assert [c["id"] for c in collapse_cards_by_file_set([used, unused], [])] == ["used"]
        assert [c["id"] for c in collapse_cards_by_file_set([unused, used], [])] == ["used"]

        early = make_card("early", ["docs/a.md", "docs/b.md"],
                          provenance={"created_by_task": "bootstrap",
                                      "created_at": "2026-01-01"})
        late = make_card("late", ["docs/a.md", "docs/b.md"],
                         provenance={"created_by_task": "bootstrap",
                                     "created_at": "2026-09-01"})
        assert [c["id"] for c in collapse_cards_by_file_set([late, early], [])] == ["early"]

    def test_an_unknown_creation_time_does_not_count_as_earliest(self):
        known = make_card("known", ["docs/a.md"],
                          provenance={"created_by_task": "bootstrap",
                                      "created_at": "2026-05-01"})
        unknown = make_card("unknown", ["docs/a.md"], provenance={"created_by_task": "bootstrap"})
        assert card_preference_key(known, 1) < card_preference_key(unknown, 0)

    def test_a_card_with_no_files_passes_through_untouched(self):
        # A conversation or run card is not file-derived, so nothing here can judge it.
        conv = make_card("from-a-run", [])
        out = collapse_cards_by_file_set([conv], [])
        assert [c["id"] for c in out] == ["from-a-run"]


# ---------------------------------------------------------------------------
# 3. One file is not a topic
# ---------------------------------------------------------------------------
class TestMinimumCardSize:
    @pytest.mark.parametrize("rel", [
        "app/config.json", "deploy/values.yaml", "deploy/values.yml",
        "app/runner_state.json", "web/package.json", "web/package-lock.json",
        "web/yarn.lock", "api/poetry.lock", "var/local_cache/thing.md",
        "node_modules/pkg/readme.md",
    ])
    def test_a_generated_data_file_is_recognised(self, rel):
        assert path_is_data_file(rel), rel

    @pytest.mark.parametrize("rel", ["docs/design.md", "app/service.py", "src/main.ts"])
    def test_authored_content_is_not_data(self, rel):
        assert not path_is_data_file(rel)

    def test_a_lone_data_file_is_never_carded(self):
        assert card_is_too_thin(make_card("c", ["app/config.json"]), set())
        assert card_is_too_thin(make_card("c", ["app/runner_state.json"]), set())

    def test_a_lone_authored_file_is_allowed_only_when_nothing_else_covers_it(self):
        card = make_card("c", ["docs/design.md"])
        assert card_is_too_thin(card, set()) == ""
        assert card_is_too_thin(card, {"docs/design.md"}) != ""

    def test_two_files_are_always_enough(self):
        assert card_is_too_thin(make_card("c", ["a/one.json", "a/two.json"]), set()) == ""

    def test_a_card_with_no_files_is_never_judged(self):
        assert card_is_too_thin(make_card("c", []), {"anything"}) == ""


# ---------------------------------------------------------------------------
# 4. Ceilings, and they are never silent
# ---------------------------------------------------------------------------
class TestCardCeilings:
    """One rule: a topic card describes a folder, so a folder gets a card.

    There is deliberately NO global budget and no per-area quota. Both existed briefly and both
    were numbers somebody picked: one card per 25 indexable files with a floor of 50, and five
    cards per area. On the two stores they were written for, the budget's only lasting effect was
    to refuse to index anything new forever, and what the number should be became a question for a
    human. The folder is not a quota, it is what a card is about.
    """

    def test_the_folder_of_a_card_is_its_files_common_directory(self):
        assert card_folder_key(make_card("c", ["a/b/one.py", "a/b/two.py"])) == "a/b"
        assert card_folder_key(make_card("c", ["a/b/one.py", "a/c/two.py"])) == "a"
        assert card_folder_key(make_card("c", ["top.py", "other.py"])) == "."
        assert card_folder_key(make_card("c", [])) == ""

    def test_one_card_per_folder(self):
        cards = [make_card("first", ["app/one.py", "app/two.py"]),
                 make_card("second", ["app/three.py", "app/four.py"])]
        kept = apply_card_ceilings(cards, [])
        assert [c["id"] for c in kept] == ["first"]

    def test_a_card_per_folder_scales_with_the_corpus_and_nothing_else(self):
        # 40 folders of real content give 40 cards. No quota anywhere decides this; the corpus
        # does. This is the test that would fail if an arbitrary budget came back.
        cards = [make_card(f"c{i}", [f"repo/service/sub{i}/one.py",
                                     f"repo/service/sub{i}/two.py"])
                 for i in range(40)]
        assert len(apply_card_ceilings(cards, [])) == 40

    def test_a_store_full_of_cards_still_indexes_a_new_folder(self):
        # The failure the removed budget caused: a store that came out of a cleanup holding more
        # cards than the budget allowed could never card anything new again.
        existing = [make_card(f"old{i}", [f"old{i}/a.py", f"old{i}/b.py"]) for i in range(500)]
        fresh = [make_card("new", ["new/a.py", "new/b.py"])]
        assert [c["id"] for c in apply_card_ceilings(fresh, existing)] == ["new"]

    def test_existing_cards_hold_their_own_folder(self):
        existing = [make_card("already", ["app/one.py", "app/two.py"])]
        fresh = [make_card("another", ["app/three.py", "app/four.py"])]
        assert apply_card_ceilings(fresh, existing) == []

    def test_a_card_with_no_files_never_counts_and_is_never_dropped(self):
        # A store full of genuinely learned conversation cards must not refuse to index a corpus.
        existing = [make_card(f"conv{i}", []) for i in range(50)]
        fresh = [make_card("topic", ["app/a.py", "app/b.py"]), make_card("learned", [])]
        kept = apply_card_ceilings(fresh, existing)
        assert sorted(c["id"] for c in kept) == ["learned", "topic"]

    def test_a_card_already_in_the_store_is_refreshed_not_dropped(self):
        existing = [make_card("known", ["app/one.py", "app/two.py"])]
        refresh = [make_card("known", ["app/one.py", "app/two.py", "app/three.py"])]
        kept = apply_card_ceilings(refresh, existing)
        assert [c["id"] for c in kept] == ["known"], (
            "refusing a refresh leaves the store stale without making it any smaller"
        )

    def test_truncation_is_logged_at_warning_and_never_silent(self, caplog):
        cards = [make_card("first", ["app/one.py", "app/two.py"]),
                 make_card("second", ["app/three.py", "app/four.py"])]
        with caplog.at_level(logging.WARNING, logger="quest-ai-runner.context"):
            apply_card_ceilings(cards, [])
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "a ceiling nobody is told about is indistinguishable from a bug"
        assert "folder" in warnings[0].getMessage()

    def test_the_per_folder_knob_raises_or_removes_the_limit(self, monkeypatch):
        cards = [make_card(f"c{i}", [f"app/one{i}.py", f"app/two{i}.py"]) for i in range(7)]
        monkeypatch.setenv("QAR_BOOTSTRAP_MAX_CARDS_PER_FOLDER", "3")
        assert len(apply_card_ceilings(cards, [])) == 3
        monkeypatch.setenv("QAR_BOOTSTRAP_MAX_CARDS_PER_FOLDER", "0")
        assert len(apply_card_ceilings(cards, [])) == 7

    def test_an_operator_max_cards_still_caps_one_pass(self, monkeypatch):
        monkeypatch.setenv("QAR_BOOTSTRAP_MAX_CARDS_PER_FOLDER", "0")
        cards = [make_card(f"c{i}", [f"app/one{i}.py", f"app/two{i}.py"]) for i in range(5)]
        assert len(apply_card_ceilings(cards, [], max_cards=2)) == 2
        # Opt-in only: with no cap passed, nothing limits the pass but the folder rule.
        assert len(apply_card_ceilings(cards, [])) == 5


# ---------------------------------------------------------------------------
# The quarantine directory is invisible to the store
# ---------------------------------------------------------------------------
class TestQuarantineIsInvisible:
    def test_a_quarantined_card_is_not_loaded_and_does_not_count(self, tmp_path):
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        (cards_dir / "keeper.json").write_text(
            json.dumps(make_card("keeper", ["a/one.py", "a/two.py"])), encoding="utf-8")
        quarantined = cards_dir / QUARANTINE_DIR_NAME / "surplus"
        quarantined.mkdir(parents=True)
        (quarantined / "gone.json").write_text(
            json.dumps(make_card("gone", ["a/one.py", "a/two.py"])), encoding="utf-8")

        repo = FilesystemCardRepository(str(cards_dir))
        assert set(repo.load_all()) == {"keeper"}
        assert repo._full_scan_stamp()[1] == 1, "the quarantined card must not be counted"

    def test_the_store_sidecars_are_not_loaded_as_cards(self, tmp_path):
        # folder_review.json used to be enumerated as a card under the id "folder_review" and
        # keyword-scored like a topic.
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        (cards_dir / "keeper.json").write_text(
            json.dumps(make_card("keeper", ["a/one.py", "a/two.py"])), encoding="utf-8")
        (cards_dir / "folder_review.json").write_text(
            json.dumps({"version": 2, "folders": {}}), encoding="utf-8")
        (cards_dir / "bootstrap_meta.json").write_text(json.dumps({"v": 1}), encoding="utf-8")
        assert set(FilesystemCardRepository(str(cards_dir)).load_all()) == {"keeper"}
