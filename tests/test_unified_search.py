"""One ranking over context cards and ESOI records (search-cards-and-specs) and its rival, the two pipelines side by side, offline."""
import json
import shutil
from pathlib import Path

import pytest

from esoi import spec_search

from quest_ai_runner.core import unified_search as us
from quest_ai_runner.core.spec_selection import load_spec_index

REPO = Path(__file__).resolve().parents[1]


def entry(kind, id_, title, summary="", refs=(), intent="demo"):
    return {"id": id_, "kind": kind, "intent": intent, "title": title, "summary": summary, "refs": list(refs),
            "terms": spec_search.terms_of(title, summary)}


# A tiny intent: one served problem with its solution, one problem nothing solves, and an intent with no solution at all.
INDEX = {"version": 1, "n": 9, "df": {}, "entries": [
    entry("intent", "demo", "Reliable nightly invoices for small shops", refs=()),
    entry("problem", "prob-invoices-late", "Invoices arrive late", "Nightly invoice batch finishes after shops open.",
          refs=("sol-invoice-queue", "ideal-on-time")),
    entry("problem", "prob-refund-disputes", "Refund disputes pile up unanswered", "Nobody owns refund disputes and customers wait weeks.",
          refs=("ideal-on-time",)),
    entry("solution", "sol-invoice-queue", "Invoice batch queue", "Queue invoice generation per shop so the nightly batch finishes early.",
          refs=("prob-invoices-late", "ideal-on-time", "metric-invoice-latency", "sol-invoice-queue:workers")),
    entry("ideal", "ideal-on-time", "On-time paperwork", "Every shop has its papers before opening.",
          refs=("prob-invoices-late", "prob-refund-disputes")),
    entry("metric", "metric-invoice-latency", "Invoice latency", "Minutes from batch start to the last invoice.",
          refs=("sol-invoice-queue",)),
    entry("implementation", "sol-invoice-queue:workers", "Worker pool", "Four workers drain the queue.", refs=("sol-invoice-queue",)),
    entry("intent", "warehouse", "Warehouse robots that restock shelves overnight", intent="warehouse"),
    entry("solution", "sol-tax-export", "Tax export file", "Export quarterly tax totals as a spreadsheet.", refs=()),
]}
CARDS = {
    "backup-keys": {"name": "Backup encryption key rotation", "summary": "How the backup encryption keys are rotated each month.",
                    "keywords": ["backup", "encryption", "rotation"]},
    "invoice-runbook": {"name": "Invoice batch runbook", "summary": "Runbook for the nightly invoice batch: restart, queue depth.",
                        "keywords": ["invoice", "batch", "queue", "runbook"]},
}
TAGS = spec_search.TagTable(tag_problems={"paperwork-risk": ("prob-refund-disputes",)},
                   solution_tags={"sol-tax-export": ("paperwork-risk",)})


def views(tag_table=None, cards=CARDS):
    return spec_search.views_from_items(cards, kind="card", source="cards") + spec_search.views_from_index(INDEX, tag_table)


def kinds(result):
    return [(i.kind, i.key) for i in result.items]


class Judge:
    def __init__(self, keep, fail=False):
        self.keep, self.fail, self.calls = list(keep), fail, 0

    def answer(self, messages, *, model, system=None, layers=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        return json.dumps({"ids": [k for k in self.keep if f" {k}:" in messages[0]["content"]] + ["invented"]})


def test_a_request_that_matches_only_a_card_returns_the_card_and_no_record():
    got = us.solutions_and_problems_with_links("rotate the backup encryption keys", views())
    assert got.items and got.items[0].key == "card:backup-keys" and got.items[0].kind == "card"
    assert all(i.source == "cards" for i in got.items) and got.no_solution_yet == "" and got.calls == 0


def test_a_request_that_matches_only_a_spec_brings_its_ideal_metric_intent_and_implementation_as_linked():
    got = us.solutions_and_problems_with_links("export quarterly tax totals spreadsheet", views(), expand=4)
    assert got.items[0].key == "sol-tax-export" and not any(i.kind == "card" for i in got.items)
    got = us.solutions_and_problems_with_links("queue invoice generation per shop", views(cards={}), expand=4)
    linked = {i.key: (i.kind, i.linked_from) for i in got.items if i.linked_from}
    assert linked["ideal-on-time"] == ("ideal", "sol-invoice-queue")
    assert linked["metric-invoice-latency"] == ("metric", "sol-invoice-queue")
    assert linked["demo"] == ("intent", "sol-invoice-queue")
    assert linked["sol-invoice-queue:workers"] == ("implementation", "sol-invoice-queue")
    ranked_kinds = {i.kind for i in got.items if not i.linked_from}
    assert ranked_kinds <= set(us.RANKED_KINDS)   # ideals, metrics, intents, implementations come only through links


def test_a_mixed_request_ranks_the_card_and_the_record_together_each_labelled():
    got = us.solutions_and_problems_with_links("the nightly invoice batch queue finishes late", views())
    ranked = {i.key: i.kind for i in got.items if not i.linked_from}
    assert ranked.get("card:invoice-runbook") == "card"
    assert ranked.get("sol-invoice-queue") == "solution" or ranked.get("prob-invoices-late") == "problem"
    text = spec_search.render(got)
    assert all(line.split(" ", 1)[0] in us.RANKED_KINDS + us.LINKED_KINDS for line in text.splitlines())


def test_a_request_that_matches_nothing_returns_nothing_and_spends_nothing():
    judge = Judge(["card:backup-keys"])
    got = us.solutions_and_problems_with_links("zebra quokka marmalade", views(), provider=judge, model="fast")
    assert got.items == [] and got.route == "nothing" and got.calls == judge.calls == 0 and spec_search.render(got) == ""


def test_one_model_call_judges_the_pool_invented_keys_are_dropped_and_a_failed_call_keeps_the_best():
    judge = Judge(["card:invoice-runbook"])
    got = us.solutions_and_problems_with_links("the nightly invoice batch queue finishes late", views(), provider=judge, model="fast")
    assert got.calls == judge.calls == 1 and [i.key for i in got.items if not i.linked_from] == ["card:invoice-runbook"]
    failed = us.solutions_and_problems_with_links("the nightly invoice batch queue finishes late", views(),
                                                  provider=Judge([], fail=True), model="fast")
    assert failed.failed_batches == 1 and failed.items


def test_an_injected_vector_search_recovers_a_card_that_shares_no_word():
    def search(text, k):
        return [("card:backup-keys", 0.9)]
    got = us.solutions_and_problems_with_links("how often do we change the secret for archives", views(), vector_search=search)
    assert "card:backup-keys" in [i.key for i in got.items]


def test_the_repository_fixture_index_answers_a_spec_only_request_with_kinds():
    index = load_spec_index(REPO / "evaluation" / "components" / "spec_selection" / "fixture_index.json")
    record_views = spec_search.views_from_index(index)
    got = us.solutions_and_problems_with_links("a rival implementation beats the selected one and nobody switches the default",
                                               record_views)
    assert got.items and all(i.source == spec_search.RECORD_SOURCE for i in got.items)
    assert {i.kind for i in got.items if not i.linked_from} <= {"solution", "problem"}


def test_card_views_come_from_a_card_repository_without_writing(tmp_path):
    from hubofhubs.recall.card_repository import FilesystemCardRepository
    from quest_ai_runner.adapters.card_views import card_views
    repo = FilesystemCardRepository(str(tmp_path))
    repo.write("backup-keys", {"id": "backup-keys", **CARDS["backup-keys"]})
    stamp = repo.revision()
    got = card_views(repo)
    assert [(v.kind, v.key, v.source) for v in got] == [("card", "card:backup-keys", "cards")] and repo.revision() == stamp


@pytest.fixture
def spec_repo(tmp_path, monkeypatch):
    pytest.importorskip("esoi.spec_index")
    shutil.copytree(REPO / "esoi", tmp_path / "esoi", ignore=shutil.ignore_patterns("tracker"))
    (tmp_path / "esoi.toml").write_text('package = "x"\nstructure_dir = "esoi"\n', encoding="utf-8")
    monkeypatch.setenv("QAR_SPEC_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("QAR_SPEC_ROOT", str(tmp_path))
    return tmp_path


def test_the_tool_sits_beside_find_specs_reads_cards_when_a_source_is_registered_and_needs_the_root(spec_repo, monkeypatch):
    from quest_ai_runner.core import tools_repo_specs
    from quest_ai_runner.core.tools import ToolContext, build_tool_registry
    spec = tools_repo_specs.REPO_SPEC_BUILDERS["search_cards_and_specs"]({})
    assert spec is not None and not spec.mutates and spec.needs_spec_root
    assert tools_repo_specs.REPO_SPEC_BUILDERS["find_specs"]({}) is not None
    records_only = spec.handler({"request": "a queued task was lost or run twice"}, ToolContext())
    assert records_only.ok and "records only" in records_only.text
    assert any(i["key"] == "prob-queued-task-falls-through" for i in records_only.data["items"])
    monkeypatch.setattr(tools_repo_specs, "card_views_source", lambda: spec_search.views_from_items(CARDS, kind="card", source="cards"))
    mixed = spec.handler({"request": "rotate the backup encryption keys", "tags": "memory"}, ToolContext())
    assert mixed.ok and "card card:backup-keys:" in mixed.text and "records only" not in mixed.text
    assert not spec.handler({"request": ""}, ToolContext()).ok
    registry = build_tool_registry({"QAR_STANDARD_TOOLS": "1"})
    assert registry.get("search_cards_and_specs") is not None
    monkeypatch.delenv("QAR_SPEC_ROOT")
    monkeypatch.chdir(spec_repo.parent)
    assert tools_repo_specs.REPO_SPEC_BUILDERS["search_cards_and_specs"]({}) is None
    assert "search_cards_and_specs" not in registry.render_deep_block("a queued task", pins=("search_cards_and_specs",))


def test_the_repository_tag_table_is_read_from_the_record_files(spec_repo):
    from quest_ai_runner.core import repo_specs
    table = repo_specs.repo_tag_table(spec_repo)
    assert "specifications" in table.tag_problems and "specifications" in table.solution_tags["spec-selection"]


def test_two_pipelines_puts_the_injected_cards_then_the_find_specs_records_side_by_side():
    from quest_ai_runner.core.spec_selection import lexical_vector_gated
    task = "nightly invoice batch queue finishes late"
    got = us.two_pipelines(task, views(), select_cards=lambda t: ["card:invoice-runbook", "invoice-runbook", "unknown-card"])
    assert kinds(got)[:2] == [("card", "card:invoice-runbook"), ("card", "card:unknown-card")]
    assert got.items[0].title == "Invoice batch runbook" and got.items[1].title == "unknown-card"
    alone = lexical_vector_gated(task, INDEX, expand=us.FIND_SPECS_EXPAND)
    assert [i.key for i in got.items[2:]] == alone.ids
    assert {i.key: i.linked_from for i in got.items if i.linked_from} == alone.linked
    assert not got.no_solution_yet and got.calls == 0
    assert all(i.kind != "card" for i in us.two_pipelines(task, views()).items)
