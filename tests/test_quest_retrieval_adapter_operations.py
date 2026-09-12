"""{"operation": "<name>", "args": {...}} on the API-based QuestRetrievalAdapter.

The API-based counterpart to a consumer's own in-process "standard functions" dispatch: lets a
read step call a real, read-only QuestClient method directly (e.g. get_insights_collection,
list_collection_entries) instead of only the four coarse "kind"s this adapter already had, without
needing a network call -- QuestClient is stubbed here (a fake with the same method names), matching
this repo's own offline-test convention (no live network, no API key).
"""
from typing import Any, Dict

from quest_ai_runner.adapters.quest_retrieval_adapter import (
    _READ_ONLY_OPERATIONS,
    QuestRetrievalAdapter,
)
from quest_ai_runner.core.adapters import Observation


class _FakeClient:
    """Minimal stand-in for QuestClient: `configured` plus whichever methods a test wires up."""

    configured = True


def _adapter(**methods) -> QuestRetrievalAdapter:
    client = _FakeClient()
    for name, fn in methods.items():
        setattr(client, name, fn)
    return QuestRetrievalAdapter(client)


def test_operation_dispatch_calls_the_real_client_method_with_its_args():
    calls = []

    def fake_get_insights_collection():
        calls.append(())
        return {"insights": [{"insight": "Sleep before 11pm improves focus."}]}

    adapter = _adapter(get_insights_collection=fake_get_insights_collection)
    obs = adapter.query({"operation": "get_insights_collection"})
    assert calls == [()]
    assert obs.kind == "query"
    assert "improves focus" in obs.text


def test_operation_dispatch_passes_args_through():
    calls = []

    def fake_list_collection_entries(collection_id: str, *, page: int = 0, page_size: int = 20):
        calls.append((collection_id, page, page_size))
        return [{"id": "entry_1"}]

    adapter = _adapter(list_collection_entries=fake_list_collection_entries)
    obs = adapter.query({"operation": "list_collection_entries",
                         "args": {"collection_id": "coll_123", "page": 2}})
    assert calls == [("coll_123", 2, 20)]
    assert obs.kind == "query"
    assert "entry_1" in obs.text


def test_operation_dispatch_rejects_a_write_method_by_name():
    # "update_goal"/"create_goal"/"mark_insight_acted_on" etc are real QuestClient methods but must
    # never be reachable from a read step -- checked by name before the client is even touched.
    adapter = _adapter(update_goal=lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a write method must never be called from operation dispatch")))
    obs = adapter.query({"operation": "update_goal", "args": {"goal_id": "g1", "fields": {}}})
    assert obs.kind == "query"
    text = obs.text or ""
    assert "cannot be called from a read step" in text


def test_operation_dispatch_rejects_an_unknown_name():
    adapter = _adapter()
    obs = adapter.query({"operation": "definitely_not_a_real_operation"})
    text = obs.text or ""
    assert "cannot be called from a read step" in text
    assert "get_insights_collection" in text  # names the real read-only catalog


def test_operation_dispatch_bad_args_reports_a_recoverable_message_not_a_raise():
    def real_shaped(collection_id: str, *, page: int = 0, page_size: int = 20):
        return []

    adapter = _adapter(list_collection_entries=real_shaped)
    obs = adapter.query({"operation": "list_collection_entries", "args": {"not_a_real_kwarg": 1}})
    assert obs.kind == "query"  # not "error": a retryable mistake, not an environment fault
    assert "describe_operation" in obs.text


def test_operation_dispatch_wraps_a_client_exception_as_an_error_observation():
    def boom():
        raise RuntimeError("upstream 500")

    adapter = _adapter(get_insights_collection=boom)
    obs = adapter.query({"operation": "get_insights_collection"})
    assert obs.kind == "error"
    assert "upstream 500" in (obs.error or "")


def test_list_operations_mentions_operation_dispatch_and_the_real_catalog():
    adapter = _adapter()
    obs = adapter.list_operations()
    assert isinstance(obs, Observation)
    assert obs.kind == "query"
    text = obs.text or ""
    assert '"operation"' in text
    assert "get_insights_collection" in text


def test_describe_operation_on_a_read_only_name_gives_usage_even_with_no_dedicated_entry():
    adapter = _adapter()
    obs = adapter.describe_operation("list_collections")
    assert obs.kind == "query"
    assert "list_collections" in obs.text


def test_every_read_only_operation_name_is_a_real_questclient_method():
    # Guards against _READ_ONLY_OPERATIONS drifting from the real client (e.g. a rename in
    # QuestClient this allowlist doesn't follow) -- every name here must resolve to a real,
    # callable attribute, not silently become "operation unavailable" for everyone.
    from quest_ai_runner.runner.quest_client import QuestClient

    real_client = QuestClient(base_url="https://example.org", api_key="qsk_test")
    missing = [name for name in _READ_ONLY_OPERATIONS
              if not callable(getattr(real_client, name, None))]
    assert not missing, f"_READ_ONLY_OPERATIONS names not on QuestClient: {missing}"
