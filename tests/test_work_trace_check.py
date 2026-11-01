"""The structural work trace check: honest counts, no leak, terminal states. Offline, no model."""
from hubofhubs.ports.trace import project

from evaluation.components.work_trace_check import check_turn, passes, score
from quest_ai_runner.core import work_trace


class Sink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


CARDS = [{"id": "c1", "title": "Secret plan", "adapter": "quest"}, {"id": "c2", "title": "Other secret", "adapter": "quest"}]
CALLS = [{"kind": "web_search", "hits": [{"url": "https://a.example.org/x"}, {"url": "https://b.example.org/y"}]},
         {"kind": "web_page", "url": "https://a.example.org/x", "ok": True},
         {"kind": "records", "cards": CARDS},
         {"kind": "links", "verdicts": ["OK", "DEAD"]}]


def run_turn(audience):
    from hubofhubs.ports.retrieval import Observation
    sink = Sink()
    trace = work_trace.start(sink, {"work_trace": {"audience": audience}})
    trace.web_started()
    trace.web_searched(Observation(kind="query", hits=[{"title": "A", "url": "https://a.example.org/x"}, {"title": "B", "url": "https://b.example.org/y"}]))
    trace.web_started()
    trace.web_page_read("https://a.example.org/x", Observation(kind="read"))
    trace.records_used([{"id": "c1", "title": "Secret plan", "adapter": "quest"}, {"id": "c2", "title": "Other secret", "adapter": "quest"}])
    trace.links_checked([{"verdict": "OK"}, {"verdict": "DEAD"}])
    trace.finish()
    return sink.events


def test_a_real_runner_trace_passes_for_the_owner_and_for_a_shared_viewer():
    assert check_turn(CALLS, run_turn("owner"), private_titles=[]) == []
    assert check_turn(CALLS, run_turn("shared"), shared=True, private_titles=["Secret plan", "Other secret"]) == []


def test_owner_titles_in_a_shared_thread_are_a_leak():
    problems = check_turn(CALLS, run_turn("owner"), shared=True, private_titles=["Secret plan"])
    assert any("private title" in p for p in problems) and any("carries items" in p for p in problems)


def test_overstated_counts_are_caught():
    events = [project({"step_id": "web", "solution_id": "search-backend", "kind": "sources", "label_key": "workTrace.web",
                       "state": "done", "counts": {"found": 9, "read": 5}}, trace_id="t").to_dict()]
    problems = check_turn(CALLS, events)
    assert any("counts.read=5" in p for p in problems) and any("counts.found=9" in p for p in problems)


def test_a_step_left_running_and_a_url_in_a_label_are_caught():
    running = project({"step_id": "web", "solution_id": "search-backend", "kind": "sources", "label_key": "workTrace.web",
                       "state": "running", "counts": {"found": 1}}, trace_id="t").to_dict()
    assert any("still running" in p for p in check_turn(CALLS, [running]))
    dirty = project({"step_id": "papers", "solution_id": "x", "kind": "papers", "label_key": "workTrace.p", "state": "done",
                     "items": [{"ref": "w", "label": "ok"}]}, trace_id="t").to_dict()
    dirty["items"][0]["label"] = "see https://x.example.org/p?token=1"
    assert any("URL" in p for p in check_turn(CALLS, [dirty]))


def test_score_is_the_share_of_passing_turns():
    good = {"calls": CALLS, "events": run_turn("owner")}
    bad = {"calls": CALLS, "events": run_turn("owner"), "shared": True, "private_titles": ["Secret plan"]}
    assert score([good, bad]) == 0.5 and passes(CALLS, good["events"]) and score([]) == 1.0
