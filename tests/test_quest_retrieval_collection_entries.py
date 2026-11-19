"""The Quest retrieval adapter reads a person's collection entries (a tracker's rows and their totals).

Before this, a read step that asked for a collection's entries got "Unknown query kind" (or an error
for an operation the menu did not list), so the planner had nothing to read and a spend question was
answered as if nothing were recorded. All offline: QuestClient's transport (_request) is stubbed.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestClient
from quest_ai_runner.body.internal_context.quest.quest_retrieval_adapter import QuestRetrievalAdapter


EXPENSE_ITEMS = [
    {"id": "e4", "createdAt": "2026-11-12T09:00:00Z", "fieldValues": {"item": "Jars and lids", "amount_usd": 640, "category": "materials"}},
    {"id": "e3", "createdAt": "2026-11-10T09:00:00Z", "fieldValues": {"item": "Starter wax", "amount_usd": 420, "category": "materials"}},
    {"id": "e2", "createdAt": "2026-11-08T09:00:00Z", "fieldValues": {"item": "Label printer", "amount_usd": 900, "category": ""}},
    {"id": "e1", "createdAt": "2026-11-05T09:00:00Z", "fieldValues": {"item": "Shipping labels", "amount_usd": 380, "category": None}},
]


def client_with(routes: Dict[str, Any]) -> QuestClient:
    """A real QuestClient whose transport answers the given paths; any other path is a test failure."""
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    calls: List[Dict[str, Any]] = []

    def fake_request(method, path, *, params=None, body=None):
        calls.append({"method": method, "path": path, "params": params})
        if path in routes:
            return routes[path]
        raise AssertionError(f"unexpected request: {method} {path}")

    client._request = fake_request  # type: ignore[assignment]
    client.calls = calls  # type: ignore[attr-defined]
    return client


def entries_page(items: List[Dict[str, Any]], *, total: Optional[int] = None, has_next: bool = False) -> Dict[str, Any]:
    return {"items": items, "pagination": {"total": len(items) if total is None else total, "page": 0,
                                           "limit": 50, "total_pages": 1, "has_next": has_next, "has_prev": False}}


EXPENSE_LOG_PATH = "/api/data/collections/coll_expense_log/entries"


def test_list_operations_names_both_collection_reads():
    text = QuestRetrievalAdapter(client_with({})).list_operations().text or ""
    assert "list_collections:" in text
    assert "list_collection_entries:" in text


def test_describe_operation_gives_the_usage_of_the_entries_read():
    obs = QuestRetrievalAdapter(client_with({})).describe_operation("list_collection_entries")
    assert obs.kind == "query"
    assert "query({operation: 'list_collection_entries', args: {collection_id:" in (obs.text or "")


def test_list_collections_gives_each_collection_name_and_id():
    client = client_with({"/api/data/collections": [
        {"id": "coll_expense_log", "name": "Launch Expenses"},
        {"id": "coll_run_log", "name": "Morning run"},
    ]})
    obs = QuestRetrievalAdapter(client).query({"operation": "list_collections"})
    assert obs.kind == "query"
    assert "- Launch Expenses (id: coll_expense_log)" in obs.text
    assert "- Morning run (id: coll_run_log)" in obs.text


def test_entries_come_back_newest_first_with_the_number_totals():
    client = client_with({EXPENSE_LOG_PATH: entries_page(EXPENSE_ITEMS)})
    obs = QuestRetrievalAdapter(client).query({"operation": "list_collection_entries",
                                               "args": {"collection_id": "coll_expense_log"}})
    assert obs.kind == "query"
    text = obs.text or ""
    assert text.startswith("4 entries in collection coll_expense_log, newest first (4 in all)")
    assert text.index("Jars and lids") < text.index("Shipping labels")
    assert "amount_usd: 900" in text
    assert "Totals of the number fields over these entries: amount_usd 2340" in text
    assert "category" not in text.split("Label printer")[1].split("\n")[0]   # a blank field is left out
    assert client.calls[0]["params"] == {"page": 0, "limit": 50}


def test_a_page_that_has_more_says_so_and_does_not_total_it():
    client = client_with({EXPENSE_LOG_PATH: entries_page(EXPENSE_ITEMS, total=120, has_next=True)})
    obs = QuestRetrievalAdapter(client).query({"operation": "list_collection_entries",
                                               "args": {"collection_id": "coll_expense_log"}})
    assert "(120 in all); more exist: read page 1 to see the rest" in obs.text
    assert "Totals of the number fields" not in obs.text


def test_a_failed_read_is_an_error_not_an_empty_collection():
    # The client answers {} when the request fails; that must not read as "no entries".
    client = client_with({EXPENSE_LOG_PATH: {}})
    obs = QuestRetrievalAdapter(client).query({"operation": "list_collection_entries",
                                               "args": {"collection_id": "coll_expense_log"}})
    assert obs.kind == "error"
    assert "could not be read" in obs.error


def test_an_entries_read_without_a_collection_id_is_named_not_sent():
    client = client_with({})
    obs = QuestRetrievalAdapter(client).query({"operation": "list_collection_entries", "args": {}})
    assert obs.kind == "error" and "collection_id" in obs.error
    assert client.calls == []


def test_a_write_or_unknown_operation_is_refused_without_a_request():
    client = client_with({})
    adapter = QuestRetrievalAdapter(client)
    for name in ("create_goal", "mark_insight_acted_on", "add_quest_note"):
        obs = adapter.query({"operation": name, "args": {}})
        assert obs.kind == "error" and "Unknown operation" in obs.error
    assert client.calls == []


def test_a_planned_spec_nests_the_operation_under_query_and_still_runs():
    # The planner nests params under "query"; the orchestrator flattens them over the spec before the
    # adapter sees it (orchestrator_reads.py). Simulate that flattening here.
    client = client_with({"/api/data/collections": [{"id": "coll_expense_log", "name": "Launch Expenses"}],
                          EXPENSE_LOG_PATH: entries_page(EXPENSE_ITEMS)})
    planned = {"query": {"operation": "list_collection_entries", "args": {"collection_id": "coll_expense_log"}}}
    payload = dict(planned)
    payload.update(planned["query"])
    obs = QuestRetrievalAdapter(client).query(payload)
    assert obs.kind == "query" and "Totals of the number fields over these entries: amount_usd 2340" in obs.text
