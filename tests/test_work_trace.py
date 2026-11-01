"""The turn's work trace: events derived from structural results, the privacy projection, and the seam wiring.

Offline: fake web, scholarly and link-guard results, no network and no model."""
import json
from types import SimpleNamespace

from hubofhubs.ports.deep import ProgressEvent
from hubofhubs.ports.events import EVENT_WORK_TRACE
from hubofhubs.ports.retrieval import Observation
from hubofhubs.ports.trace import validate

from quest_ai_runner.core import work_trace
from quest_ai_runner.core.orchestrator_reads import ReadsMixin
from quest_ai_runner.core.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec


class Sink:
    def __init__(self):
        self.events = []

    def emit(self, event: ProgressEvent):
        self.events.append(event)

    def latest(self):
        by_step = {}
        for e in self.events:
            assert e.type == EVENT_WORK_TRACE
            assert validate(e.data) == [], e.data
            by_step[e.data["step_id"]] = e.data
        return by_step


def make(audience="owner"):
    sink = Sink()
    return sink, work_trace.start(sink, {"work_trace": {"audience": audience}})


def test_off_unless_the_consumer_opts_in():
    assert work_trace.start(Sink(), None) is None
    assert work_trace.start(Sink(), {"quest_ids": ["a"]}) is None


def test_web_search_and_page_read_use_counts_and_domains_only():
    sink, trace = make()
    trace.web_started()
    obs = Observation(kind="query", hits=[
        {"title": "Sleep and memory", "url": "https://www.example.org/a/b?token=SECRET", "snippet": "PRIVATE SNIPPET", "query": "my secret query"},
        {"title": "Other", "url": "https://news.example.com/x", "snippet": "s"}])
    trace.web_searched(obs)
    trace.web_started()
    trace.web_page_read("https://news.example.com/x?utm=1", Observation(kind="read", text="PAGE BODY"))
    trace.web_started()
    trace.web_page_read("https://down.example.net/p", Observation(kind="error", error="timeout"))
    step = sink.latest()["web"]
    assert step["kind"] == "sources" and step["solution_id"] == "search-backend" and step["state"] == "done"
    assert step["counts"] == {"found": 2, "read": 1, "failed": 1}
    assert [i["ref"] for i in step["items"]] == ["news.example.com", "example.org"]
    blob = json.dumps([e.data for e in sink.events])
    for forbidden in ("SECRET", "PRIVATE SNIPPET", "my secret query", "PAGE BODY", "/a/b", "timeout"):
        assert forbidden not in blob


def test_all_failed_web_step_is_failed_and_running_while_in_flight():
    sink, trace = make()
    trace.web_started()
    assert sink.latest()["web"]["state"] == "running"
    trace.web_searched(Observation(kind="error", error="boom"))
    assert sink.latest()["web"]["state"] == "failed"


def test_scholarly_result_becomes_citation_lines():
    sink, trace = make()
    trace.papers_started()
    works = [{"title": "Memory consolidation", "authors": ["Ana Lee", "Bo Kim", "Cy Roe"], "year": 2021,
              "doi": "https://doi.org/10.1000/xyz", "abstract": "PRIVATE ABSTRACT"},
             {"title": "Sleep", "authors": ["Dee Fox"], "year": 2019, "doi": None}]
    trace.papers_searched(ToolResult(ok=True, text="TEXT", data={"works": works, "total": 31}))
    step = sink.latest()["papers"]
    assert step["counts"] == {"found": 31, "read": 2} and step["kind"] == "papers"
    assert step["items"][0] == {"ref": "10.1000/xyz", "label": "Memory consolidation", "sub": "Lee et al., 2021"}
    assert step["items"][1]["sub"] == "Fox, 2019"
    assert "PRIVATE ABSTRACT" not in json.dumps(step)


def test_scholarly_failure():
    sink, trace = make()
    trace.papers_started()
    trace.papers_searched(ToolResult(ok=False, text="failed"))
    assert sink.latest()["papers"]["state"] == "failed"


CARDS = [{"id": "c1", "title": "Run a marathon", "adapter": "quest", "item_type_counts": {"quest": 1}},
         {"id": "c2", "title": "Morning pages", "adapter": "quest", "item_type_counts": {"reflection": 2}},
         {"id": "c3", "title": "Conversation turn", "adapter": "turn"},
         {"id": "c4", "title": "Hi there", "adapter": "turn"}]


def test_records_titles_go_to_the_owner_only():
    sink, trace = make("owner")
    trace.records_used(CARDS)
    step = sink.latest()["records"]
    assert step["counts"] == {"found": 2, "read": 2}
    assert [(i["label"], i.get("kind")) for i in step["items"]] == [("Run a marathon", "quest"), ("Morning pages", "reflection")]


def test_records_are_counts_only_in_a_shared_thread_or_unknown_audience():
    for audience in ("shared", "", "someone"):
        sink, trace = make(audience)
        trace.records_used(CARDS)
        step = sink.latest()["records"]
        assert step["items"] == [] and step["counts"]["found"] == 2
        assert "marathon" not in json.dumps([e.data for e in sink.events])


def test_link_check_counts_only_and_affects_user_when_one_was_removed():
    sink, trace = make()
    trace.links_checked([{"url": "https://a.example.org/p?token=SECRET", "verdict": "OK"},
                         {"url": "https://b.example.org/p", "verdict": "DEAD", "reason": "404"}])
    step = sink.latest()["links"]
    assert step["kind"] == "check" and step["counts"] == {"checked": 2, "failed": 1} and step["affects_user"] is True
    assert step["items"] == [] and "SECRET" not in json.dumps(step)
    sink2, trace2 = make()
    trace2.links_checked([])
    assert sink2.events == []


def test_finish_closes_a_running_step_and_a_broken_emit_never_raises():
    sink, trace = make()
    trace.web_started()
    trace.finish()
    assert sink.latest()["web"]["state"] != "running"

    class Broken:
        def emit(self, event):
            raise RuntimeError("stream dropped")
    broken = work_trace.start(Broken(), {"work_trace": {"audience": "owner"}})
    broken.web_started()
    broken.links_checked(None)
    broken.records_used("not a list")


class FakeWeb:
    def search(self, queries, fresh=False, **kw):
        return Observation(kind="query", hits=[{"title": "T", "url": "https://one.example.org/p"}])

    def fetch(self, url, **kw):
        return Observation(kind="read", text="body")


class Host(ReadsMixin):
    def __init__(self, tools=None):
        self.web = FakeWeb()
        self.tools = tools
        self.retrieval = None
        self.guidance = None
        self.cfg = SimpleNamespace(max_reads_per_step=5, max_gather_chars=1000, max_parallel=2)


def test_seam_web_reads_emit_through_the_card_context():
    sink, trace = make()
    ctx = SimpleNamespace(trace=trace)
    host = Host()
    host._exec_one_read({"web": "anything"}, None, ctx)
    host._exec_one_read({"web_page": "https://one.example.org/p"}, None, ctx)
    step = sink.latest()["web"]
    assert step["counts"] == {"found": 1, "read": 1} and step["state"] == "done"
    # No trace on the context: the read behaves as before.
    assert host._exec_one_read({"web": "q"}, None, SimpleNamespace()).kind == "query"
    assert host._exec_one_read({"web": "q"}, None, None).kind == "query"


def test_seam_tool_call_for_the_literature_search_emits():
    sink, trace = make()
    spec = ToolSpec(name=work_trace.SCHOLARLY_TOOL, description="d", when_to_use="w", when_not_to_use="n",
                    parameters={"type": "object", "properties": {"query": {"type": "string"}}},
                    handler=lambda args, ctx: ToolResult(ok=True, text="t", data={"works": [{"title": "A paper", "authors": ["X Y"], "year": 2020}], "total": 5}),
                    mutates=False, origin="standard")
    host = Host(ToolRegistry([spec]))
    gathered = []
    emitter = SimpleNamespace(emit=lambda e: None, status=lambda t: None)
    host.run_tool_calls([{"name": work_trace.SCHOLARLY_TOOL, "args": {"query": "sleep"}}], gathered=gathered,
                        exec_record=SimpleNamespace(facts=[]), tool_ok_signatures=set(), emit=emitter, steps=1,
                        ctx=ToolContext(), trace=trace)
    step = sink.latest()["papers"]
    assert step["counts"] == {"found": 5, "read": 1} and step["state"] == "done"
