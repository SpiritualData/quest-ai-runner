"""Reflections — the person's own words, read from Quest and surfaced where decisions get made.

Three layers, all offline (no network, no key): the two ``QuestClient`` methods against a stubbed
``_request``, the ``collect_reflections`` composer against a fake client, and the retrieval
adapter's ``reflection_context`` query kind, which is what an attended chat's planner can actually
issue. The gap this closes was observed live: asked to choose work "based on my daily reflection",
the assistant correctly refused to invent one and asked the person to paste it, because no action
existed that could go and read it.
"""
from datetime import datetime, timezone

from quest_ai_runner.adapters.quest_retrieval_adapter import QuestRetrievalAdapter
from quest_ai_runner.runner.quest_client import QuestApiError, QuestClient
from quest_ai_runner.runner.reflections import ReflectionContext, collect_reflections

NOW = datetime(2026, 8, 12, 9, 0, 0, tzinfo=timezone.utc)


# --- QuestClient methods ----------------------------------------------------------------------

def client_capturing_calls(responses):
    """A QuestClient whose transport returns canned responses keyed by path, recording each call."""
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    calls = []

    def fake_request(method, path, *, params=None, body=None):
        calls.append({"method": method, "path": path, "params": params})
        value = responses.get(path)
        if isinstance(value, Exception):
            raise value
        return value

    client._request = fake_request  # type: ignore[assignment]
    return client, calls


def test_get_daily_reflection_parses_the_real_response_shape():
    client, calls = client_capturing_calls({
        "/api/daily-plan/today": {
            "has_plan": True, "plan_id": "entry_1", "date": "2026-08-12",
            "yesterday_review": "Shipped the migration, but lost the afternoon to meetings.",
            "today_plan": "Two hours on the retrieval adapter before anything else.",
            "goals_created": 3,
        },
    })
    payload = client.get_daily_reflection()
    assert calls[0]["method"] == "GET"
    assert calls[0]["path"] == "/api/daily-plan/today"
    # No date means "today in the user's own timezone", resolved server-side.
    assert calls[0]["params"] is None
    assert payload["yesterday_review"].startswith("Shipped the migration")
    assert payload["today_plan"].startswith("Two hours")


def test_get_daily_reflection_passes_an_explicit_date_through():
    client, calls = client_capturing_calls({"/api/daily-plan/today": {"has_plan": False}})
    client.get_daily_reflection(date="2026-08-10")
    assert calls[0]["params"] == {"date": "2026-08-10"}


def test_get_daily_reflection_degrades_to_empty_on_a_404():
    client, _ = client_capturing_calls({
        "/api/daily-plan/today": QuestApiError("Quest API GET /api/daily-plan/today -> 404: no",
                                               status=404),
    })
    assert client.get_daily_reflection() == {}


def test_get_period_reflection_returns_the_review_block_and_drops_stats():
    client, calls = client_capturing_calls({
        "/api/period-review/week/current": {
            "stats": {"completions": 11, "time_distribution": {"quest_a": 4}},
            "review": {
                "has_review": True, "review_id": "rev_1", "status": "completed",
                "reflection_past": "Steady week, the writing goal slipped again.",
                "reflection_future": "Protect two mornings for writing.",
            },
        },
    })
    review = client.get_period_reflection("week")
    assert calls[0]["path"] == "/api/period-review/week/current"
    assert review["reflection_future"] == "Protect two mornings for writing."
    assert "stats" not in review


def test_get_period_reflection_sends_use_previous_and_timezone():
    client, calls = client_capturing_calls({
        "/api/period-review/month/current": {"review": {"has_review": False}},
    })
    client.get_period_reflection("month", use_previous=True, tz="America/New_York")
    assert calls[0]["params"] == {"use_previous": "true", "timezone": "America/New_York"}


def test_get_period_reflection_reports_an_unsubmitted_review_rather_than_failing():
    client, _ = client_capturing_calls({
        "/api/period-review/week/current": {"stats": {}, "review": {"has_review": False}},
    })
    review = client.get_period_reflection("week")
    assert review == {"has_review": False}


def test_get_period_reflection_rejects_an_unknown_period_without_a_request():
    client, calls = client_capturing_calls({})
    # "day" is NOT a period-review period (the daily equivalent is get_daily_reflection), and the
    # endpoint would 422. Catch it client-side rather than spending the round trip.
    assert client.get_period_reflection("day") == {}
    assert calls == []


def test_get_period_reflection_degrades_to_empty_on_an_api_error():
    client, _ = client_capturing_calls({
        "/api/period-review/week/current": QuestApiError("boom", status=500),
    })
    assert client.get_period_reflection("week") == {}


# --- collect_reflections ------------------------------------------------------------------------

class FakeReflectionClient:
    """A Quest client stand-in exposing only the two reflection reads."""

    def __init__(self, daily_by_date=None, reviews=None):
        # {date-or-None: payload}; None is the "today, user's timezone" call.
        self.daily_by_date = dict(daily_by_date or {})
        # {(period, use_previous): review payload}
        self.reviews = dict(reviews or {})
        self.daily_calls = []
        self.period_calls = []

    def get_daily_reflection(self, *, date=None):
        self.daily_calls.append(date)
        return dict(self.daily_by_date.get(date) or {"has_plan": False})

    def get_period_reflection(self, period, *, use_previous=False, tz=None):
        self.period_calls.append((period, use_previous))
        return dict(self.reviews.get((period, use_previous)) or {"has_review": False})


def test_collect_reflections_gathers_both_daily_and_period():
    client = FakeReflectionClient(
        daily_by_date={None: {"has_plan": True, "date": "2026-08-12",
                              "yesterday_review": "Lost the afternoon to meetings.",
                              "today_plan": "Two hours on the adapter."}},
        reviews={("week", False): {"has_review": True,
                                   "reflection_past": "The writing goal slipped again.",
                                   "reflection_future": "Protect two mornings for writing."}},
    )
    ctx = collect_reflections(client, now=NOW)
    assert ctx.has_daily() and ctx.has_period()
    text = ctx.as_text()
    assert "Lost the afternoon to meetings." in text
    assert "Protect two mornings for writing." in text
    assert "2026-08-12" in text          # the reflection is dated, never presented as timeless
    assert "current week" in text


def test_collect_reflections_with_only_a_daily_entry():
    client = FakeReflectionClient(
        daily_by_date={None: {"has_plan": True, "date": "2026-08-12",
                              "yesterday_review": "Good deep-work day."}},
    )
    ctx = collect_reflections(client, now=NOW)
    assert ctx.has_daily() and not ctx.has_period()
    assert "Good deep-work day." in ctx.as_text()
    # Every period it looked at is recorded, so a caller can say "checked, nothing there".
    assert ctx.checked_periods == ["week", "month"]


def test_collect_reflections_with_neither_is_empty_not_an_error():
    ctx = collect_reflections(FakeReflectionClient(), now=NOW)
    assert not ctx.has_any()
    assert ctx.as_text() == ""
    assert ctx.one_line() == ""


def test_collect_reflections_walks_back_when_today_has_no_entry_yet():
    """The morning case: today's plan is not written yet, so yesterday's entry is the newest thing
    the person wrote. Without the walk-back an early pass looks blind on a person who reflects
    daily."""
    client = FakeReflectionClient(
        daily_by_date={"2026-08-11": {"has_plan": True, "date": "2026-08-11",
                                      "yesterday_review": "Finally unblocked the deploy."}},
    )
    ctx = collect_reflections(client, now=NOW)
    assert client.daily_calls[:2] == [None, "2026-08-11"]
    assert ctx.daily_date == "2026-08-11"
    assert "Finally unblocked the deploy." in ctx.as_text()


def test_collect_reflections_falls_back_to_the_previous_period():
    """Early in a week, this week has no review but last week's is the person's newest word."""
    client = FakeReflectionClient(
        reviews={("week", True): {"has_review": True,
                                  "reflection_past": "Three good sessions, one wasted day."}},
    )
    ctx = collect_reflections(client, now=NOW)
    assert ctx.period == "week"
    assert "previous week" in ctx.as_text()
    assert ("week", False) in client.period_calls and ("week", True) in client.period_calls


def test_collect_reflections_honors_the_caller_s_period_order():
    client = FakeReflectionClient(
        reviews={("quarter", False): {"has_review": True, "reflection_past": "Quarter went wide."},
                 ("week", False): {"has_review": True, "reflection_past": "Week went fine."}},
    )
    ctx = collect_reflections(client, periods=("quarter", "week"), now=NOW)
    assert ctx.period == "quarter"
    assert "Quarter went wide." in ctx.as_text()


def test_collect_reflections_skips_a_submitted_but_empty_review():
    """A review row with both boxes blank is not an answer; stopping on it would hide a real one."""
    client = FakeReflectionClient(
        reviews={("week", False): {"has_review": True, "reflection_past": "",
                                   "reflection_future": None},
                 ("month", False): {"has_review": True, "reflection_future": "Ship the beta."}},
    )
    ctx = collect_reflections(client, now=NOW)
    assert ctx.period == "month"
    assert "Ship the beta." in ctx.as_text()


def test_collect_reflections_tolerates_a_client_without_the_methods():
    class OldClient:
        pass

    ctx = collect_reflections(OldClient(), now=NOW)
    assert isinstance(ctx, ReflectionContext) and not ctx.has_any()


def test_collect_reflections_never_raises_when_a_read_blows_up():
    class ExplodingClient:
        def get_daily_reflection(self, *, date=None):
            raise RuntimeError("transport down")

        def get_period_reflection(self, period, *, use_previous=False, tz=None):
            raise RuntimeError("transport down")

    assert not collect_reflections(ExplodingClient(), now=NOW).has_any()


def test_a_very_long_reflection_is_truncated_with_a_marker():
    client = FakeReflectionClient(
        daily_by_date={None: {"has_plan": True, "date": "2026-08-12",
                              "yesterday_review": "x" * 5000}},
    )
    ctx = collect_reflections(client, now=NOW)
    assert "[...truncated]" in ctx.yesterday_review
    assert len(ctx.yesterday_review) < 1400


def test_one_line_condenses_for_the_next_steps_artifact():
    ctx = ReflectionContext(daily_date="2026-08-12", yesterday_review="Lost the day to meetings.")
    assert ctx.one_line() == "From their daily reflection (2026-08-12): Lost the day to meetings."


# --- the retrieval adapter's query kind ---------------------------------------------------------

class ConfigurableFakeClient(FakeReflectionClient):
    """Adds the ``configured`` property the adapter gates every query on."""
    configured = True


def adapter_with(**kwargs):
    return QuestRetrievalAdapter(ConfigurableFakeClient(**kwargs))


def test_reflection_context_query_returns_both_reflections():
    adapter = adapter_with(
        daily_by_date={None: {"has_plan": True, "date": "2026-08-12",
                              "yesterday_review": "Lost the afternoon to meetings."}},
        reviews={("week", False): {"has_review": True,
                                   "reflection_future": "Protect two mornings for writing."}},
    )
    obs = adapter.query({"kind": "reflection_context"})
    assert obs.kind == "query"
    assert obs.rel_path == "quest://reflections"
    assert "Lost the afternoon to meetings." in obs.text
    assert "Protect two mornings for writing." in obs.text


def test_reflection_context_query_with_only_the_daily_entry():
    adapter = adapter_with(
        daily_by_date={None: {"has_plan": True, "date": "2026-08-12",
                              "yesterday_review": "Good deep-work day."}},
    )
    obs = adapter.query({"kind": "reflection_context"})
    assert obs.kind == "query"
    assert "Good deep-work day." in obs.text


def test_reflection_context_query_reports_absence_as_a_result_not_an_error():
    """kind="error" would read as "this lookup is broken" and send the planner straight back to
    asking the person to paste text it has just verified does not exist."""
    obs = adapter_with().query({"kind": "reflection_context"})
    assert obs.kind == "query"
    assert "No reflection is recorded" in obs.text
    assert "week, month" in obs.text


def test_reflection_context_query_accepts_a_single_period_string():
    adapter = adapter_with(
        reviews={("quarter", False): {"has_review": True,
                                      "reflection_past": "Quarter went wide."}},
    )
    obs = adapter.query({"kind": "reflection_context", "periods": "quarter",
                         "include_daily": False})
    assert "Quarter went wide." in obs.text
    assert adapter.client.daily_calls == []


def test_the_reflection_operation_is_discoverable_in_the_menu():
    adapter = adapter_with()
    ops = adapter.list_operations()
    assert "get_reflection_context:" in ops.text
    detail = adapter.describe_operation("get_reflection_context")
    assert detail.kind == "query"
    # Concrete enough to call without guessing, the same bar get_goal_context is held to.
    assert "reflection_context" in detail.text and "query({" in detail.text
