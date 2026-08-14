"""Insights — the person's own captures, read from Quest and surfaced with their own tags.

Two layers, both offline (no network, no key): the three ``QuestClient`` methods against a stubbed
``_request``, and ``collect_unacted_insights`` against a fake client. The selection this proves is
entirely CLIENT-side, because the entries endpoint has no server-side filter for either half of it
(``acted_on`` or a date), and because the composed block deliberately does no matching at all
between an insight's category tags and any quest — the tags are shown to the reader and the reader
judges, which is the whole point of the design.
"""
from datetime import datetime, timedelta, timezone

from quest_ai_runner.runner.insights import (
    DEFAULT_WINDOW_DAYS,
    InsightsContext,
    collect_unacted_insights,
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


# --- the composer -------------------------------------------------------------------------------

def _entry(entry_id, text, *, categories=None, acted_on=False, created_at=None, camel=True):
    """One entry AS THE ENTRIES ENDPOINT RETURNS IT: camelCase envelope, field ids inside."""
    values = {"insight": text, "acted_on": acted_on}
    if categories is not None:
        values["categories"] = categories
    created = created_at if created_at is not None else NOW
    if camel:
        return {"id": entry_id, "fieldValues": values,
                "createdAt": created if isinstance(created, str)
                else created.isoformat().replace("+00:00", "Z")}
    return {"id": entry_id, "field_values": values, "created_at": created}


class FakeInsightsClient:
    """A client exposing only the two read methods ``collect_unacted_insights`` needs."""

    def __init__(self, pages=None, collection=None, fail_entries=False):
        self.pages = list(pages or [])        # a list of {items, pagination} payloads, by page
        self.collection = collection if collection is not None else {"id": "coll_1"}
        self.fail_entries = fail_entries
        self.entry_calls = []
        self.collection_calls = 0

    def get_insights_collection(self):
        self.collection_calls += 1
        return dict(self.collection)

    def list_collection_entries(self, collection_id, *, page=0, limit=50):
        self.entry_calls.append((collection_id, page, limit))
        if self.fail_entries:
            raise RuntimeError("entries endpoint down")
        return self.pages[page] if page < len(self.pages) else {"items": []}


def _page(entries, *, has_next=False):
    return {"items": list(entries), "pagination": {"has_next": has_next}}


def test_acted_on_insights_are_filtered_out():
    client = FakeInsightsClient(pages=[_page([
        _entry("e1", "Mornings are the only time the writing happens"),
        _entry("e2", "Already handled this one", acted_on=True),
    ])])
    ctx = collect_unacted_insights(client, now=NOW)
    assert [i.entry_id for i in ctx.insights] == ["e1"]
    assert ctx.collection_id == "coll_1"


def test_the_acted_on_checkbox_is_read_through_its_string_forms():
    """A JSON round-trip can leave "true"/"false" behind; a stray "false" must not read as ticked."""
    client = FakeInsightsClient(pages=[_page([
        _entry("e1", "Still open", acted_on="false"),
        _entry("e2", "Closed", acted_on="true"),
    ])])
    assert [i.entry_id for i in collect_unacted_insights(client, now=NOW).insights] == ["e1"]


def test_the_since_cutoff_drops_anything_captured_before_it():
    client = FakeInsightsClient(pages=[_page([
        _entry("new", "Captured this morning", created_at=NOW - timedelta(hours=2)),
        _entry("old", "Captured last week", created_at=NOW - timedelta(days=6)),
    ])])
    ctx = collect_unacted_insights(client, since=NOW - timedelta(days=1), now=NOW)
    assert [i.entry_id for i in ctx.insights] == ["new"]


def test_both_timestamp_shapes_are_understood():
    """The HTTP response carries an ISO string; an in-process caller can hand back a datetime."""
    client = FakeInsightsClient(pages=[_page([
        _entry("iso", "From the wire", created_at="2026-08-12T07:00:00Z"),
        _entry("dt", "From storage", created_at=NOW - timedelta(hours=3), camel=False),
        _entry("stale-iso", "Older, on the wire", created_at="2026-08-01T07:00:00Z"),
    ])])
    ctx = collect_unacted_insights(client, since=NOW - timedelta(days=1), now=NOW)
    assert [i.entry_id for i in ctx.insights] == ["iso", "dt"]


def test_an_offset_timestamp_is_converted_rather_than_stamped():
    """Stamping UTC onto an aware value (as a naive ``replace`` would) moves the capture by hours,
    which is enough to push a late-evening insight across a cutoff."""
    client = FakeInsightsClient(pages=[_page([
        _entry("late", "Written last night", created_at="2026-08-11T23:30:00-07:00"),
    ])])
    # 2026-08-11T23:30-07:00 IS 2026-08-12T06:30Z, which is after a midnight-UTC cutoff.
    ctx = collect_unacted_insights(client, since=datetime(2026, 8, 12, tzinfo=timezone.utc), now=NOW)
    assert [i.entry_id for i in ctx.insights] == ["late"]


def test_an_unreadable_timestamp_keeps_the_insight():
    """Matching quest-backend: a capture with a malformed date is still something the person wrote."""
    client = FakeInsightsClient(pages=[_page([_entry("e1", "No date on this", created_at="???")])])
    ctx = collect_unacted_insights(client, since=NOW - timedelta(days=1), now=NOW)
    assert [i.entry_id for i in ctx.insights] == ["e1"]


def test_the_default_window_bounds_a_missing_cutoff():
    client = FakeInsightsClient(pages=[_page([
        _entry("recent", "This week", created_at=NOW - timedelta(days=2)),
        _entry("ancient", "Three months ago", created_at=NOW - timedelta(days=90)),
    ])])
    ctx = collect_unacted_insights(client, now=NOW)
    assert [i.entry_id for i in ctx.insights] == ["recent"]
    assert ctx.window_days == DEFAULT_WINDOW_DAYS


def test_a_very_old_since_is_still_bounded_by_the_window():
    client = FakeInsightsClient(pages=[_page([
        _entry("ancient", "A year ago", created_at=NOW - timedelta(days=365)),
    ])])
    ctx = collect_unacted_insights(client, since=NOW - timedelta(days=400), days_cap=14, now=NOW)
    assert ctx.insights == []


def test_the_list_is_capped_and_paging_stops():
    entries = [_entry(f"e{n}", f"Insight {n}", created_at=NOW - timedelta(minutes=n))
               for n in range(20)]
    client = FakeInsightsClient(pages=[_page(entries, has_next=True), _page(entries)])
    ctx = collect_unacted_insights(client, max_insights=5, now=NOW)
    assert len(ctx.insights) == 5
    # Enough in hand from page 0: no second page is fetched despite has_next.
    assert client.entry_calls == [("coll_1", 0, 50)]


def test_paging_stops_once_a_page_reaches_past_the_cutoff():
    """Entries come back newest-first, so the first one past the cutoff ends the walk."""
    client = FakeInsightsClient(pages=[
        _page([_entry("new", "Fresh", created_at=NOW - timedelta(hours=1)),
               _entry("old", "Stale", created_at=NOW - timedelta(days=30))], has_next=True),
        _page([_entry("older", "Staler", created_at=NOW - timedelta(days=40))]),
    ])
    ctx = collect_unacted_insights(client, now=NOW)
    assert [i.entry_id for i in ctx.insights] == ["new"]
    assert client.entry_calls == [("coll_1", 0, 50)]


def test_paging_continues_while_the_server_says_there_is_more():
    fresh = [_entry(f"p0_{n}", f"Page zero {n}", created_at=NOW - timedelta(minutes=n))
             for n in range(3)]
    more = [_entry(f"p1_{n}", f"Page one {n}", created_at=NOW - timedelta(minutes=10 + n))
            for n in range(2)]
    client = FakeInsightsClient(pages=[_page(fresh, has_next=True), _page(more, has_next=False)])
    ctx = collect_unacted_insights(client, max_insights=8, now=NOW)
    assert len(ctx.insights) == 5
    assert client.entry_calls == [("coll_1", 0, 50), ("coll_1", 1, 50)]


def test_a_client_without_the_insight_methods_yields_an_empty_context():
    class Bare:
        pass

    ctx = collect_unacted_insights(Bare(), now=NOW)
    assert not ctx.has_any() and ctx.as_text() == "" and ctx.collection_id == ""


def test_a_missing_collection_yields_an_empty_context():
    client = FakeInsightsClient(collection={})
    ctx = collect_unacted_insights(client, now=NOW)
    assert not ctx.has_any()
    assert client.entry_calls == []


def test_a_blown_up_entries_read_yields_an_empty_context():
    client = FakeInsightsClient(pages=[_page([_entry("e1", "Never seen")])], fail_entries=True)
    ctx = collect_unacted_insights(client, now=NOW)
    assert not ctx.has_any()


def test_a_blown_up_collection_read_yields_an_empty_context():
    class Exploding(FakeInsightsClient):
        def get_insights_collection(self):
            raise RuntimeError("Quest insights endpoint down")

    assert not collect_unacted_insights(Exploding(), now=NOW).has_any()


# --- what the block actually says -----------------------------------------------------------

def test_as_text_shows_each_insight_with_the_persons_own_tags():
    client = FakeInsightsClient(pages=[_page([
        _entry("e1", "Mornings are the only time the writing happens",
               categories=["dissertation", "energy"], created_at=NOW - timedelta(hours=2)),
        _entry("e2", "Stop scheduling calls before noon", categories=[],
               created_at=NOW - timedelta(days=1)),
    ])])
    text = collect_unacted_insights(client, now=NOW).as_text()
    assert "Mornings are the only time the writing happens" in text
    assert "tagged dissertation, energy" in text
    assert "(untagged)" in text          # an untagged capture is still shown, not dropped
    assert "[2026-08-12]" in text and "[2026-08-11]" in text


def test_as_text_hands_the_relevance_judgment_to_the_reader():
    """The tags are context for the model, never a filter in this code: nothing here compares a
    category against a quest or goal name, and the block says so explicitly."""
    client = FakeInsightsClient(pages=[_page([
        _entry("e1", "Batch the errands", categories=["home"]),
    ])])
    text = collect_unacted_insights(client, now=NOW).as_text()
    assert "Judge for yourself which of them (if any) bear on the goals above" in text
    assert "can still matter here" in text


def test_an_empty_context_says_nothing_at_all():
    assert InsightsContext().as_text() == "" and InsightsContext().one_line() == ""


def test_a_comma_separated_tags_string_is_accepted():
    client = FakeInsightsClient(pages=[_page([
        _entry("e1", "Voice-captured", categories="health, career"),
    ])])
    assert collect_unacted_insights(client, now=NOW).insights[0].categories == ["health", "career"]


def test_a_long_insight_is_clipped_with_a_visible_marker():
    client = FakeInsightsClient(pages=[_page([_entry("e1", "x" * 900)])])
    text = collect_unacted_insights(client, now=NOW).insights[0].text
    assert text.endswith("[...truncated]") and len(text) < 500


def test_one_line_condenses_the_newest_capture():
    client = FakeInsightsClient(pages=[_page([
        _entry("e1", "Mornings are the only time the writing happens",
               categories=["dissertation"], created_at=NOW - timedelta(hours=1)),
        _entry("e2", "Second one", created_at=NOW - timedelta(hours=5)),
    ])])
    line = collect_unacted_insights(client, now=NOW).one_line()
    assert line.startswith("Unacted insight from 2026-08-12 [dissertation]:")
    assert line.endswith("(+1 more)")


# --- narrowing one fetch to many per-quest cutoffs --------------------------------------------

def test_narrow_to_filters_an_already_collected_context():
    client = FakeInsightsClient(pages=[_page([
        _entry("new", "This morning", created_at=NOW - timedelta(hours=2)),
        _entry("old", "Four days ago", created_at=NOW - timedelta(days=4)),
    ])])
    ctx = collect_unacted_insights(client, now=NOW)
    assert len(ctx.insights) == 2
    narrowed = ctx.narrow_to(NOW - timedelta(days=1))
    assert [i.entry_id for i in narrowed.insights] == ["new"]
    assert "since 2026-08-11" in narrowed.as_text()
    # The original is untouched, so one fetch can serve many different cutoffs.
    assert len(ctx.insights) == 2


def test_narrow_to_none_keeps_everything():
    client = FakeInsightsClient(pages=[_page([_entry("e1", "Anything")])])
    ctx = collect_unacted_insights(client, now=NOW)
    assert ctx.narrow_to(None) is ctx


def test_narrow_to_a_cutoff_older_than_the_window_does_not_overstate_the_reach():
    client = FakeInsightsClient(pages=[_page([
        _entry("e1", "Two days ago", created_at=NOW - timedelta(days=2)),
    ])])
    ctx = collect_unacted_insights(client, days_cap=14, now=NOW)
    narrowed = ctx.narrow_to(NOW - timedelta(days=400))
    assert len(narrowed.insights) == 1
    # It is labeled with the window it actually had, not the year-old cutoff it was asked for.
    assert "since 2026-07-29" in narrowed.as_text()
