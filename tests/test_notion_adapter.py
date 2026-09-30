"""NotionAdapter: a read-only retrieval adapter over configured Notion databases.

What this file pins down:

  * READ-ONLY BY CONSTRUCTION. Across every method the adapter has, the only requests that ever
    reach the wire are GETs and the one POST Notion uses to QUERY a database; a write verb is
    refused in ``request`` before a socket opens, and the module source contains no write verbs.
  * NEVER RAISES. A missing token, a 401, a 500, a network failure and garbage JSON all come back
    as ``Observation(kind="error")``, and no error text carries the token.
  * THE FAKE IS NOTION, NOT AN ECHO: it paginates with opaque cursors and a 100 row page cap,
    rejects filters that do not have Notion's shape, and reports edit times at minute granularity,
    so these tests fail where the real API would.
  * ONLY CONFIGURED DATABASES ARE READ: an alias or id that is not configured, and a page whose
    parent is not a configured database, are refused.

Offline: ``urlopen`` is replaced by ``tests/notion_fake.py``.
"""
import inspect
import urllib.error

import pytest

from quest_ai_runner.adapters import notion_adapter as notion_module
from quest_ai_runner.adapters.notion_adapter import (
    NOTION_VERSION,
    NotionAdapter,
    NotionError,
    build_notion_filter,
    dashed,
    env_token_provider,
    file_token_provider,
    normalize_id,
    static_token_provider,
)
from quest_ai_runner.adapters.reference_resolver import collect_reference_resolvers

from .notion_fake import (
    DB_ID,
    OTHER_DB_ID,
    SCHEMA,
    FakeNotion,
    heading,
    make_page,
    paragraph,
)

PAGE_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PAGE_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
PAGE_OUT = "cccccccccccccccccccccccccccccccc"


@pytest.fixture
def fake(monkeypatch):
    notion = FakeNotion()
    notion.add(make_page(PAGE_A, "Write the methods section", status="In progress",
                         deadline="2026-10-01", tags=["writing"], edited="2026-09-18T10:00:00Z"))
    notion.add(make_page(PAGE_B, "Recruit participants", status="Not started", tags=["study"],
                         edited="2026-09-10T10:00:00Z"))
    notion.set_blocks(PAGE_A, [heading("Plan"), paragraph("Draft the sampling paragraph first.")])
    monkeypatch.setattr("urllib.request.urlopen", notion)
    return notion


def adapter(**kw):
    kw.setdefault("token_provider", static_token_provider("test-token"))
    kw.setdefault("database_ids", {"tasks": DB_ID})
    return NotionAdapter(**kw)


# --- the surface -------------------------------------------------------------------------------

def test_list_sources_names_configured_databases_without_any_request(fake):
    obs = adapter().list_sources()
    assert obs.kind == "query" and "tasks" in obs.text and DB_ID in obs.text
    assert fake.requests == []


def test_describe_source_lists_property_types(fake):
    obs = adapter().describe_source("tasks")
    assert obs.kind == "query"
    assert "Status: status" in obs.text and "Deadline Date: date" in obs.text


def test_query_with_a_simple_filter_sends_a_notion_shaped_filter(fake):
    obs = adapter().query({"database": "tasks", "filter": {"Status": "In progress"}})
    assert obs.kind == "query"
    assert "Write the methods section" in obs.text and "Recruit participants" not in obs.text
    (_path, body), = fake.queries
    assert body["filter"] == {"property": "Status", "status": {"equals": "In progress"}}


def test_a_date_and_a_tag_filter_combine_with_and(fake):
    obs = adapter().query({"database": "tasks", "filter": {
        "Deadline Date": {"on_or_after": "2026-09-01"}, "Tags": "writing"}})
    assert obs.kind == "query" and "Write the methods section" in obs.text
    (_path, body), = fake.queries
    assert body["filter"] == {"and": [
        {"property": "Deadline Date", "date": {"on_or_after": "2026-09-01"}},
        {"property": "Tags", "multi_select": {"contains": "writing"}}]}


def test_every_filter_build_is_accepted_by_the_notion_fake():
    """The builder's output shapes are checked against the fake's model of Notion's validation."""
    fake = FakeNotion()
    for simple in ({"Name": "methods"}, {"Points": {"greater_than": 2}}, {"Done": False},
                   {"Status": {"is_not_empty": True}}, {"Tags": {"does_not_contain": "x"}}):
        fake.check_filter(build_notion_filter({k: {"type": t} for k, t in SCHEMA.items()}, simple))


def test_a_bad_property_or_operator_is_an_error_naming_the_valid_choices(fake):
    a = adapter()
    bad_property = a.query({"database": "tasks", "filter": {"Stauts": "x"}})
    assert bad_property.kind == "error" and "properties are" in bad_property.error
    bad_operator = a.query({"database": "tasks", "filter": {"Status": {"contains": "x"}}})
    assert bad_operator.kind == "error" and "use one of" in bad_operator.error
    assert fake.queries == []          # refused before a query was sent


def test_query_ranks_rows_by_keyword_overlap(fake):
    obs = adapter().query({"database": "tasks", "query": "recruit participants"})
    assert obs.kind == "query"
    assert obs.text.index("Recruit participants") == 0


def test_a_single_configured_database_is_the_default(fake):
    assert adapter().query({"limit": 5}).kind == "query"


def test_several_databases_require_naming_one(fake):
    a = adapter(database_ids={"tasks": DB_ID, "people": OTHER_DB_ID})
    obs = a.query({})
    assert obs.kind == "error" and "people" in obs.error and "tasks" in obs.error


def test_an_unconfigured_database_is_refused_by_alias_and_by_id(fake):
    a = adapter()
    for name in ("elsewhere", OTHER_DB_ID):
        obs = a.query({"database": name})
        assert obs.kind == "error" and "not one of the configured databases" in obs.error
    assert fake.requests == []


def test_grep_finds_rows_across_properties_and_honours_scope(fake):
    a = adapter()
    hit = a.grep("in progress")
    assert hit.kind == "grep" and hit.hits[0]["file"] == f"tasks/{PAGE_A}"
    assert a.grep("nope-nothing").kind == "error"
    assert a.grep("recruit", scope="missing").kind == "error"
    assert a.grep("(").kind == "error"


def test_read_section_returns_properties_and_block_text(fake):
    obs = adapter().read_section(PAGE_A)
    assert obs.kind == "read"
    assert "Status: In progress" in obs.text and "Deadline Date: 2026-10-01" in obs.text
    assert "## Plan" in obs.text and "Draft the sampling paragraph first." in obs.text


def test_read_section_accepts_a_link_a_dashed_id_and_alias_slash_id(fake):
    a = adapter()
    for ref in (f"https://www.notion.so/Write-the-methods-{PAGE_A}", dashed(PAGE_A), f"tasks/{PAGE_A}"):
        assert a.read_section(ref).kind == "read", ref


def test_a_page_outside_the_configured_databases_is_refused(fake):
    fake.add(make_page(PAGE_OUT, "Somebody else's page", database=OTHER_DB_ID), database=OTHER_DB_ID)
    obs = adapter().read_section(PAGE_OUT)
    assert obs.kind == "error" and "not in one of the configured databases" in obs.error
    assert "Somebody else's page" not in (obs.text or "")


def test_page_text_is_bounded_and_says_so(fake):
    fake.set_blocks(PAGE_A, [paragraph("x" * 100) for _ in range(150)])
    short = adapter(max_blocks=10).read_section(PAGE_A)
    assert "[truncated" in short.text and short.text.count("x" * 100) == 10
    capped = adapter().read_section(PAGE_A, max_bytes=500)
    assert len(capped.text) <= 520 and capped.text.endswith("[truncated]")


def test_block_children_paginate_with_cursors(fake):
    fake.set_blocks(PAGE_A, [paragraph(f"block {i}") for i in range(130)])
    obs = adapter(max_blocks=200, max_page_chars=100000).read_section(PAGE_A)
    assert "block 0" in obs.text and "block 129" in obs.text
    child_calls = [p for m, p, _b in fake.requests if "/children" in p]
    assert len(child_calls) == 2 and "start_cursor=" in child_calls[1]


# --- pagination and filters as Notion models them ---------------------------------------------

def many_rows(fake, count):
    for i in range(count):
        page_id = f"{i:032x}"
        fake.add(make_page(page_id, f"Row {i}", status="Not started",
                           edited=f"2026-08-{1 + i % 28:02d}T10:{i % 60:02d}:00Z"))


def test_fetch_rows_follows_cursors_and_never_exceeds_the_page_cap(fake):
    many_rows(fake, 130)
    result = adapter(max_rows_per_database=500).fetch_rows("tasks")
    assert len(result.rows) == 132 and not result.truncated
    bodies = [b for _p, b in fake.queries]
    assert len(bodies) == 2 and all(b["page_size"] <= 100 for b in bodies)
    assert "start_cursor" not in bodies[0] and bodies[1]["start_cursor"].startswith("cursor:")


def test_fetch_rows_reports_truncation_when_more_matched_than_the_limit(fake):
    many_rows(fake, 130)
    result = adapter().fetch_rows("tasks", limit=120)
    assert len(result.rows) == 120 and result.truncated


def test_rows_come_back_most_recently_edited_first(fake):
    rows = adapter().fetch_rows("tasks").rows
    assert [r.page_id for r in rows] == [PAGE_A, PAGE_B]


def test_edited_since_filters_on_last_edited_time_floored_to_the_minute(fake):
    from datetime import datetime, timezone
    since = datetime(2026, 9, 18, 10, 0, 30, tzinfo=timezone.utc)
    rows = adapter().fetch_rows("tasks", edited_since=since).rows
    # the row edited at 10:00:45 is stored as 10:00:00 by Notion; the floored filter still finds it
    assert [r.page_id for r in rows] == [PAGE_A]
    (_path, body), = fake.queries
    assert body["filter"] == {"timestamp": "last_edited_time",
                              "last_edited_time": {"on_or_after": "2026-09-18T10:00:00.000Z"}}


def test_archived_rows_are_left_out(fake):
    fake.add(make_page("d" * 32, "Old", archived=True))
    assert "d" * 32 not in [r.page_id for r in adapter().fetch_rows("tasks").rows]


# --- never raises, never leaks the token -------------------------------------------------------

def every_method(a):
    return [
        a.read_section(PAGE_A), a.grep("x"), a.query({"database": "tasks"}), a.list_sources(),
        a.describe_source("tasks"), a.describe_source("nope"), a.list_operations(),
        a.describe_operation("notion_read"), a.describe_operation("zzz"),
        a.make_locator(PAGE_A), a.resolve_reference({"page_id": PAGE_A}),
    ]


@pytest.mark.parametrize("failure", [
    urllib.error.URLError("unreachable"),
    TimeoutError("slow"),
    lambda: urllib.error.HTTPError("u", 500, "boom", {}, None),
    lambda: urllib.error.HTTPError("u", 403, "forbidden", {}, None),
])
def test_no_method_raises_when_the_network_fails(fake, failure):
    fake.fail_with = failure
    results = every_method(adapter())
    assert all(r is None or isinstance(r, (dict, str)) or r.kind in ("error", "query") for r in results)


def test_garbage_json_is_an_error_not_a_crash(monkeypatch):
    class Garbage:
        def __call__(self, req, timeout=None):
            class R:
                def read(s): return b"<html>not json</html>"
                def __enter__(s): return s
                def __exit__(s, *e): return False
            return R()
    monkeypatch.setattr("urllib.request.urlopen", Garbage())
    obs = adapter().query({"database": "tasks"})
    assert obs.kind == "error" and "not JSON" in obs.error


def test_unconfigured_adapters_report_a_clean_error(fake):
    for a in (NotionAdapter(), NotionAdapter(token_provider=static_token_provider("t")),
              NotionAdapter(database_ids={"tasks": DB_ID})):
        assert a.read_section(PAGE_A).kind == "error"
        assert a.grep("x").kind == "error"
        assert a.query({}).kind == "error"
        assert "not configured" in a.list_sources().text
    no_token = adapter(token_provider=lambda: None)
    assert no_token.query({"database": "tasks"}).kind == "error"
    assert fake.requests == []


def test_the_token_never_appears_in_an_error(fake):
    fake.token = "a-different-token"                  # so the adapter's token is rejected with a 401
    obs = adapter(token_provider=static_token_provider("secret-value-123")).query({"database": "tasks"})
    assert obs.kind == "error" and "401" in obs.error
    assert "secret-value-123" not in repr(obs)


def test_the_pinned_version_and_bearer_token_are_sent(fake):
    adapter().query({"database": "tasks"})
    headers = fake.headers[0]
    assert headers["notion-version"] == NOTION_VERSION == "2022-06-28"
    assert headers["authorization"] == "Bearer test-token"


# --- read-only, proven ---------------------------------------------------------------------------

def test_no_request_but_a_get_or_a_database_query_ever_reaches_the_wire(fake):
    a = adapter()
    every_method(a)
    a.fetch_rows("tasks")
    a.read_page(PAGE_A)
    assert fake.writes == []
    for method, path, _body in fake.requests:
        assert method == "GET" or (method == "POST" and path.endswith("/query")), (method, path)


@pytest.mark.parametrize("method,path", [
    ("POST", "/pages"), ("PATCH", f"/pages/{dashed(PAGE_A)}"), ("DELETE", f"/blocks/{dashed(PAGE_A)}"),
    ("PUT", "/pages"), ("POST", f"/blocks/{dashed(PAGE_A)}/children"),
    ("POST", f"/databases/{dashed(DB_ID)}"), ("POST", "/search"), ("GET", "/users"),
])
def test_a_write_or_unlisted_request_is_refused_before_any_socket_opens(fake, method, path):
    with pytest.raises(NotionError, match="refused"):
        adapter().request(method, path, {"x": 1} if method != "GET" else None)
    assert fake.requests == []


def test_the_module_source_contains_no_write_verbs():
    source = inspect.getsource(notion_module)
    for verb in ("PATCH", "PUT", "DELETE"):
        assert f'"{verb}"' not in source and f"'{verb}'" not in source
    assert source.count('"POST"') == 2          # the guard's allowance and its one call site
    assert "def create" not in source and "def update" not in source and "def delete" not in source


# --- learned references ------------------------------------------------------------------------

def test_make_locator_and_resolve_reference_round_trip_fresh(fake):
    a = adapter()
    a.fetch_rows("tasks")
    locator = a.make_locator(PAGE_A)
    assert locator == {"page_id": PAGE_A, "database": "tasks"}
    first = a.resolve_reference(locator)
    fake.set_blocks(PAGE_A, [paragraph("Changed since the reference was learned.")])
    assert "Changed since" in a.resolve_reference(locator) and "Changed since" not in first


def test_resolve_reference_is_none_for_a_bad_locator_a_refused_page_or_a_failure(fake):
    fake.add(make_page(PAGE_OUT, "Not ours", database=OTHER_DB_ID), database=OTHER_DB_ID)
    a = adapter()
    assert a.make_locator("") == {} and a.make_locator("not an id") == {}
    assert a.resolve_reference({}) is None
    assert a.resolve_reference({"page_id": PAGE_OUT}) is None
    fake.fail_with = urllib.error.URLError("down")
    assert a.resolve_reference({"page_id": PAGE_A}) is None


def test_the_adapter_advertises_a_resolvable_reference_type(fake):
    a = adapter()
    assert a.reference_type == "notion_page"
    assert collect_reference_resolvers(a) == {"notion_page": a.resolve_reference}


# --- token providers and ids ------------------------------------------------------------------

def test_token_providers_read_an_env_var_name_or_a_file(monkeypatch, tmp_path):
    monkeypatch.setenv("QAR_TEST_NOTION_TOKEN", "  from-env \n")
    assert env_token_provider("QAR_TEST_NOTION_TOKEN")() == "from-env"
    monkeypatch.delenv("QAR_TEST_NOTION_TOKEN")
    assert env_token_provider("QAR_TEST_NOTION_TOKEN")() is None
    assert env_token_provider("")() is None
    token_file = tmp_path / "tok"
    token_file.write_text("from-file\n")
    assert file_token_provider(str(token_file))() == "from-file"
    assert file_token_provider(str(tmp_path / "missing"))() is None
    assert static_token_provider("")() is None


def test_normalize_id_handles_links_dashes_and_garbage():
    assert normalize_id(dashed(PAGE_A)) == PAGE_A
    assert normalize_id(f"https://www.notion.so/ws/Title-With-Words-{PAGE_A}?pvs=4") == PAGE_A
    assert normalize_id(f"tasks/{PAGE_A}") == PAGE_A
    assert normalize_id("") == "" and normalize_id("hello") == "" and normalize_id(None) == ""
