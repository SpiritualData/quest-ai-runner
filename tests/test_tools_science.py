"""The standard tool search_scientific_literature: offered only with an OpenAlex key, wired to the hub's OpenAlex arm, and the
planner is told it is unavailable when it is not offered. Offline: the OpenAlex reply is a fake transport (invented works)."""
from quest_ai_runner.core.tools import ToolContext, build_tool_registry
from quest_ai_runner.core.tools_science import TOOL_NAME

WORKS = {"meta": {"count": 2, "cost_usd": 0.001}, "results": [
    {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.5555/demo.one", "display_name": "Easy cycling during knee rehabilitation",
     "publication_year": 2021, "cited_by_count": 12, "authorships": [{"author": {"display_name": "Author One"}}],
     "primary_location": {"landing_page_url": "https://example.org/one", "source": {"display_name": "Journal of Demos"}},
     "open_access": {"is_oa": True, "oa_status": "gold"}, "abstract_inverted_index": {"Cycling": [0], "helped": [1]}},
    {"id": "https://openalex.org/W2", "doi": None, "display_name": "A paper without a DOI", "publication_year": 2019,
     "cited_by_count": 0, "authorships": [], "primary_location": {"landing_page_url": "https://example.org/two", "source": None},
     "open_access": {"is_oa": False}, "abstract_inverted_index": None}]}


class Response:
    status_code, headers = 200, {}

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class Transport:
    def __init__(self, response):
        self.response, self.calls = response, []

    def __call__(self, url, *, params, headers, timeout):
        self.calls.append((url, params, headers))
        return self.response


def registry_with(transport, env=None):
    from quest_ai_runner.core.tools_science import science_literature_spec
    registry = build_tool_registry({"QAR_STANDARD_TOOLS": "1"})
    registry.register(science_literature_spec(env or {"OPENALEX_API_KEY": "key-for-tests"}, transport=transport))
    return registry


def test_without_a_key_the_tool_is_not_offered():
    assert build_tool_registry({}).get(TOOL_NAME) is None
    assert build_tool_registry({"OPENALEX_API_KEY": "  "}).get(TOOL_NAME) is None


def test_with_a_key_the_tool_is_in_the_catalog_read_only_and_not_a_workflow_step():
    spec = build_tool_registry({"OPENALEX_API_KEY": "key-for-tests"}).get(TOOL_NAME)
    assert spec is not None and spec.mutates is False and spec.workflow_step is False and spec.required() == ["query"]


def test_the_planner_is_told_it_cannot_search_papers_only_on_a_request_about_papers():
    registry = build_tool_registry({})
    about_papers = registry.render_planner_block("What do studies say about easy cycling? Cite two papers.")
    assert "NOT AVAILABLE HERE: search_scientific_literature" in about_papers and "from memory" in about_papers
    assert "NOT AVAILABLE HERE" not in registry.render_planner_block("Add a goal to call the physio on Friday")


def test_no_unavailable_note_once_the_tool_is_configured():
    registry = build_tool_registry({"OPENALEX_API_KEY": "key-for-tests"})
    assert "NOT AVAILABLE HERE" not in registry.render_planner_block("cite two papers on cycling")
    assert TOOL_NAME in registry.render_planner_block("cite two papers on cycling")


def test_the_call_lists_each_work_with_its_doi_link_and_says_only_these_may_be_cited():
    transport = Transport(Response(WORKS))
    result = registry_with(transport).invoke(TOOL_NAME, {"query": "cycling knee", "year_from": 2015, "open_access_only": True}, ToolContext())
    assert result.ok and "https://doi.org/10.5555/demo.one" in result.text and "Cite only the works listed here" in result.text
    assert "No DOI listed" in result.text and "Cycling helped" in result.text
    assert [w["doi"] for w in result.data["works"]] == ["10.5555/demo.one", ""]
    url, params, headers = transport.calls[0]
    assert params["filter"] == "from_publication_date:2015-01-01,is_oa:true" and headers["Authorization"] == "Bearer key-for-tests"


def test_a_service_failure_comes_back_as_a_failed_result_that_forbids_citing_from_memory():
    class Down(Response):
        status_code = 429

    result = registry_with(Transport(Down({}))).invoke(TOOL_NAME, {"query": "x"}, ToolContext())
    assert not result.ok and "[rate_limited]" in result.text and "from memory" in result.text


def test_no_matches_says_so():
    result = registry_with(Transport(Response({"meta": {"count": 0}, "results": []}))).invoke(TOOL_NAME, {"query": "zzz"}, ToolContext())
    assert result.ok and "no matching works" in result.text


def test_the_daily_limit_comes_from_the_environment_name_and_stops_real_searches():
    transport = Transport(Response(WORKS))
    registry = registry_with(transport, {"OPENALEX_API_KEY": "key-for-tests", "QAR_SCIENCE_DAILY_LIMIT": "1"})
    assert registry.invoke(TOOL_NAME, {"query": "one"}, ToolContext()).ok
    refused = registry.invoke(TOOL_NAME, {"query": "two"}, ToolContext())
    assert not refused.ok and "[budget]" in refused.text and len(transport.calls) == 1
