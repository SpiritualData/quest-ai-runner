"""Insights — the person's own captures, read from Quest and surfaced with their own tags.

Two layers, both offline (no network, no key): the ``QuestClient`` methods against a stubbed
``_request``, and ``collect_quest_insights`` against a fake client. Context assembly reads ONE
quest's insights through the by-quest route and never the person's whole backlog; the composed
block does no matching between an insight's category tags and any quest.
"""
from datetime import datetime, timedelta, timezone

from quest_ai_runner.runner.insights import (
    DEFAULT_WINDOW_DAYS,
    InsightsContext,
    block_header,
    collect_quest_insights,
)
from quest_ai_runner.runner.quest_client import QuestApiError, QuestClient

NOW = datetime(2026, 8, 12, 9, 0, 0, tzinfo=timezone.utc)


# --- QuestClient methods ------------------------------------------------------------------------

def client_capturing_calls(responses):
    """A QuestClient whose transport returns canned responses keyed by path, recording each call."""
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    calls = []

    def fake_request(method, path, *, params=None, body=None):
        calls.append({"method": method, "path": path, "params": params, "body": body})
        value = responses.get(path)
        if isinstance(value, Exception):
            raise value
        return value

    client._request = fake_request  # type: ignore[assignment]
    return client, calls


def test_get_insights_collection_returns_the_collection_dict():
    client, calls = client_capturing_calls({
        "/api/data/insights/collection": {"id": "coll_1", "name": "Insights",
                                          "systemType": "insights_collection"},
    })
    coll = client.get_insights_collection()
    assert calls[0] == {"method": "GET", "path": "/api/data/insights/collection",
                        "params": None, "body": None}
    assert coll["id"] == "coll_1"


def test_get_insights_collection_degrades_to_empty_on_a_404():
    client, _ = client_capturing_calls({
        "/api/data/insights/collection": QuestApiError("not found", status=404),
    })
    assert client.get_insights_collection() == {}


def test_list_collection_entries_sends_page_and_limit():
    client, calls = client_capturing_calls({
        "/api/data/collections/coll_1/entries": {"items": [], "pagination": {"total": 0}},
    })
    payload = client.list_collection_entries("coll_1", page=2, limit=25)
    assert calls[0]["params"] == {"page": 2, "limit": 25}
    assert payload["pagination"]["total"] == 0


def test_list_collection_entries_degrades_to_empty_on_a_transport_failure():
    client, _ = client_capturing_calls({
        "/api/data/collections/coll_1/entries": QuestApiError("unreachable"),
    })
    assert client.list_collection_entries("coll_1") == {}


def test_mark_insight_acted_on_sends_the_documented_body():
    client, calls = client_capturing_calls({"/api/data/insights/mark-acted-on": {"success": True}})
    assert client.mark_insight_acted_on("entry_1", "coll_1", "created the goal 'Draft ch. 2'")
    assert calls[0]["method"] == "PATCH"
    assert calls[0]["body"] == {"entry_id": "entry_1", "collection_id": "coll_1",
                                "action_taken_description": "created the goal 'Draft ch. 2'"}


def test_mark_insight_acted_on_reports_failure_rather_than_swallowing_it():
    """A ticked insight drops out of every unacted list, including the weekly review's, so a caller
    must never read "I asked" as "it is done"."""
    client, _ = client_capturing_calls({
        "/api/data/insights/mark-acted-on": QuestApiError("entry not found", status=404),
    })
    assert client.mark_insight_acted_on("entry_1", "coll_1", "did the thing") is False
    # And a half-specified call never reaches the network at all.
    client2, calls2 = client_capturing_calls({})
    assert client2.mark_insight_acted_on("", "coll_1") is False
    assert calls2 == []


def test_list_quest_insights_sends_the_documented_query():
    client, calls = client_capturing_calls({
        "/api/data/insights/by-quest": {"collection_id": "c1", "has_quests_field": True,
                                         "items": []},
    })
    payload = client.list_quest_insights("q1", limit=7)
    assert calls[0]["method"] == "GET"
    assert calls[0]["params"] == {"quest_id": "q1", "limit": 7, "include_acted": "false"}
    assert payload["has_quests_field"] is True
    client.list_quest_insights("q1", include_acted=True)
    assert calls[1]["params"]["include_acted"] == "true"


def test_list_quest_insights_degrades_to_empty_on_an_older_backend_404():
    client, _ = client_capturing_calls({
        "/api/data/insights/by-quest": QuestApiError("not found", status=404),
    })
    assert client.list_quest_insights("q1") == {}


def test_list_quest_insights_with_no_quest_makes_no_request():
    client, calls = client_capturing_calls({})
    assert client.list_quest_insights("") == {}
    assert calls == []


# --- the composer -------------------------------------------------------------------------------

def _entry(entry_id, text, *, categories=None, acted_on=False, created_at=None, camel=True):
    """One entry AS THE ENDPOINT RETURNS IT: camelCase envelope, field ids inside."""
    values = {"insight": text, "acted_on": acted_on, "quests": ["q1"]}
    if categories is not None:
        values["categories"] = categories
    created = created_at if created_at is not None else NOW
    if camel:
        return {"id": entry_id, "fieldValues": values,
                "createdAt": created if isinstance(created, str)
                else created.isoformat().replace("+00:00", "Z")}
    return {"id": entry_id, "field_values": values, "created_at": created}


class FakeQuestInsightsClient:
    """Exposes ONLY the by-quest read. Any other call is an AttributeError, so a test fails if
    collection reviews the person's whole backlog."""

    def __init__(self, items=None, payload=None, fail=False):
        self.items = list(items or [])
        self.payload = payload
        self.fail = fail
        self.calls = []

    def list_quest_insights(self, quest_id, limit=10, include_acted=False):
        self.calls.append((quest_id, limit, include_acted))
        if self.fail:
            raise RuntimeError("down")
        if self.payload is not None:
            return self.payload
        return {"collection_id": "coll_1", "has_quests_field": True, "items": self.items}


def test_only_the_quest_route_is_called_with_the_quest_id():
    client = FakeQuestInsightsClient([_entry("e1", "one")])
    ctx = collect_quest_insights(client, "q1", now=NOW)
    assert client.calls == [("q1", 8, False)]
    assert ctx.quest_id == "q1" and ctx.collection_id == "coll_1"
    assert [i.entry_id for i in ctx.insights] == ["e1"]


def test_a_request_with_no_quest_gets_nothing_and_reads_nothing():
    client = FakeQuestInsightsClient([_entry("e1", "one")])
    for blank in ("", None, "  "):
        assert not collect_quest_insights(client, blank, now=NOW).has_any()
    assert client.calls == []


def test_an_older_backend_404_degrades_to_empty():
    assert not collect_quest_insights(FakeQuestInsightsClient(payload={}), "q1", now=NOW).has_any()


def test_a_collection_without_a_quests_field_is_empty():
    client = FakeQuestInsightsClient(payload={"collection_id": "c", "has_quests_field": False,
                                              "items": []})
    assert not collect_quest_insights(client, "q1", now=NOW).has_any()


def test_a_client_without_the_method_or_that_raises_yields_empty():
    class Bare:
        pass
    assert not collect_quest_insights(Bare(), "q1", now=NOW).has_any()
    assert not collect_quest_insights(FakeQuestInsightsClient(fail=True), "q1", now=NOW).has_any()


def test_acted_on_entries_are_skipped_defensively():
    client = FakeQuestInsightsClient([_entry("a", "done", acted_on=True),
                                      _entry("b", "open"),
                                      _entry("c", "stringy", acted_on="false")])
    assert [i.entry_id for i in collect_quest_insights(client, "q1", now=NOW).insights] == ["b", "c"]


def test_window_cap_and_since_cutoffs():
    old = NOW - timedelta(days=40)
    recent = NOW - timedelta(days=3)
    client = FakeQuestInsightsClient([_entry("r", "recent", created_at=recent),
                                      _entry("o", "old", created_at=old)])
    ctx = collect_quest_insights(client, "q1", now=NOW)
    assert [i.entry_id for i in ctx.insights] == ["r"]
    assert ctx.window_days == DEFAULT_WINDOW_DAYS and ctx.since is None
    ctx = collect_quest_insights(client, "q1", since=NOW - timedelta(days=1), now=NOW)
    assert not ctx.has_any() and ctx.since == NOW - timedelta(days=1)
    # A very old `since` is bounded by the window, never the whole history.
    ctx = collect_quest_insights(client, "q1", since=NOW - timedelta(days=400), now=NOW)
    assert [i.entry_id for i in ctx.insights] == ["r"]
    ctx = collect_quest_insights(client, "q1", days_cap=1, now=NOW)
    assert not ctx.has_any()


def test_newest_first_bounded_and_both_casings_read():
    items = [_entry("a", "older", created_at=NOW - timedelta(days=2), camel=False),
             _entry("b", "newest", created_at=NOW - timedelta(hours=1)),
             _entry("c", "middle", created_at=NOW - timedelta(days=1))]
    ctx = collect_quest_insights(FakeQuestInsightsClient(items), "q1", now=NOW, max_insights=2)
    assert [i.entry_id for i in ctx.insights] == ["b", "c"]


def test_an_unreadable_timestamp_is_kept_and_empty_text_dropped():
    items = [_entry("a", "no date", created_at="not-a-date"), _entry("b", "   ")]
    ctx = collect_quest_insights(FakeQuestInsightsClient(items), "q1", now=NOW)
    assert [i.entry_id for i in ctx.insights] == ["a"]


def test_tags_are_shown_verbatim_text_is_clipped_and_no_em_dash_in_the_frame():
    long = "x" * 900
    items = [_entry("a", long, categories="health, career")]
    ctx = collect_quest_insights(FakeQuestInsightsClient(items), "q1", now=NOW)
    assert ctx.insights[0].categories == ["health", "career"]
    assert ctx.insights[0].text.endswith("[...truncated]")
    text = ctx.as_text()
    assert "assigned to THIS quest" in text and "tagged health, career" in text
    assert "\u2014" not in text and "\u2014" not in block_header(None)
    assert "(+" not in ctx.one_line()


def test_empty_context_renders_nothing():
    ctx = InsightsContext()
    assert ctx.as_text() == "" and ctx.one_line() == ""


def test_collect_unacted_insights_no_longer_exists():
    """The review-all path is gone on purpose; context assembly must not be able to call it."""
    import quest_ai_runner.runner.insights as mod
    assert not hasattr(mod, "collect_unacted_insights")


# --- the executor's context view -----------------------------------------------------------

def test_executor_reads_the_tasks_quest_insights_only():
    from quest_ai_runner.runner.executor import TaskExecutor
    client = FakeQuestInsightsClient([_entry("e1", "assigned to this quest", created_at=NOW)])
    ex = TaskExecutor(client, None)
    text = ex._fetch_quest_captures("q1")
    assert client.calls == [("q1", 8, False)]      # only the quest route; no all-entries listing
    assert isinstance(text, str)


def test_executor_with_no_quest_gets_no_insights_and_reads_nothing():
    from quest_ai_runner.runner.executor import TaskExecutor
    client = FakeQuestInsightsClient([_entry("e1", "x")])
    ex = TaskExecutor(client, None)
    assert ex._fetch_quest_captures(None) == ""
    assert ex._fetch_quest_captures("") == ""
    assert client.calls == []
    assert not hasattr(ex, "_fetch_person_captures")


def test_withheld_count_from_a_shared_quest_is_told_to_the_run_not_hidden():
    payload = {"collection_id": "coll_1", "has_quests_field": True, "withheld": 2,
               "items": [_entry("a", "ship the report")]}
    ctx = collect_quest_insights(FakeQuestInsightsClient(payload=payload), "q1", now=NOW)
    text = ctx.as_text()
    assert ctx.withheld == 2 and "2 more assigned to this quest were held back" in text
    assert "—" not in text
    # An older backend sends no count, and a malformed one reads as none.
    for body in ({"items": [_entry("a", "x")]}, {"withheld": "lots", "items": [_entry("a", "x")]}):
        assert collect_quest_insights(FakeQuestInsightsClient(payload=body), "q1", now=NOW).withheld == 0
