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


REPLY = "Secret plan and Other secret are the two quests to protect; the page at a.example.org has the release notes."


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
    trace.finish(REPLY)
    return sink.events


def test_a_real_runner_trace_passes_for_the_owner_and_for_a_shared_viewer():
    assert check_turn(CALLS, run_turn("owner"), private_titles=[]) == []
    assert check_turn(CALLS, run_turn("shared"), shared=True, private_titles=["Secret plan", "Other secret"]) == []


def test_owner_titles_in_a_shared_thread_are_a_leak():
    problems = check_turn(CALLS, run_turn("owner"), shared=True, private_titles=["Secret plan"])
    assert any("private title" in p for p in problems) and any("not visible to everyone" in p for p in problems)


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


def records_event(*refs):
    return project({"step_id": "records", "solution_id": "context-assembler", "kind": "records", "label_key": "workTrace.records.looking",
                    "state": "done", "counts": {"found": len(refs), "read": len(refs)},
                    "items": [{"ref": r, "label": f"Plan {r}"} for r in refs]}, trace_id="t").to_dict()


def test_a_records_count_that_includes_unused_cards_is_caught():
    calls = [{"kind": "records", "cards": [{"id": "c1", "used": True}, {"id": "c2", "used": False}, {"id": "c3", "used": False}]}]
    problems = check_turn(calls, [records_event("c1", "c2", "c3")])
    assert any("counts.read=3" in p for p in problems) and any("did not use (c2)" in p for p in problems)
    assert check_turn(calls, [records_event("c1")]) == []


def test_a_records_step_when_no_card_was_used_is_caught_and_no_step_passes():
    calls = [{"kind": "records", "cards": [{"id": "c1", "used": False}, {"id": "c2", "used": False}]}]
    assert any("no record was used" in p for p in check_turn(calls, [records_event("c1", "c2")]))
    assert check_turn(calls, []) == []


def test_papers_read_may_not_exceed_the_works_the_reply_cited():
    calls = [{"kind": "papers", "total": 31, "works": [{"cited": True}, {"cited": False}, {"cited": False}]}]
    event = project({"step_id": "papers", "solution_id": "know-the-world", "kind": "papers", "label_key": "workTrace.papers.searching",
                     "state": "done", "counts": {"found": 31, "read": 3}}, trace_id="t").to_dict()
    assert any("counts.read=3 but the run only did 1" in p for p in check_turn(calls, [event]))
    event["counts"]["read"] = 1
    assert check_turn(calls, [event]) == []


def web_event(items, found=5, read=0):
    return project({"step_id": "web", "solution_id": "search-backend", "kind": "sources", "label_key": "workTrace.web.reading", "state": "done",
                    "counts": {"found": found, "read": read}, "items": items}, trace_id="t").to_dict()


WEB_CALLS = [{"kind": "web_search", "hits": [{"url": f"https://s{i}.example/x"} for i in range(5)]}]


def test_a_site_the_reply_does_not_cite_is_caught_and_a_no_page_opened_state_passes():
    listed = web_event([{"ref": "s1.example", "label": "S1", "kind": "found"}, {"ref": "s2.example", "label": "S2", "kind": "found"}])
    reply = "I searched but did not open a page, so I cannot confirm this. s1.example came up in the search."
    # no page opened: the listed sites are the list, each labelled found, so an uncited one is fine
    assert check_turn(WEB_CALLS, [listed], reply=reply) == []
    # beside an opened page, a site only listed must still be cited
    mixed = web_event([{"ref": "s0.example", "label": "S0", "kind": "read"}, {"ref": "s2.example", "label": "S2", "kind": "found"}], read=1)
    assert "web: lists a site the reply does not cite (s2.example)" in check_turn(WEB_CALLS, [mixed], reply=reply)
    # a site shown as read although nothing was opened is caught
    wrong = web_event([{"ref": "s1.example", "label": "S1", "kind": "read"}])
    assert any("shows a site as read although no page was opened" in p for p in check_turn(WEB_CALLS, [wrong], reply=reply))
    assert check_turn(WEB_CALLS, [web_event([])], reply=reply) == []       # found count only: searched, no page opened
    assert check_turn(WEB_CALLS, [listed]) == []                          # without the reply the check cannot judge the list


def records_titled(label, ref="c1"):
    return project({"step_id": "records", "solution_id": "context-assembler", "kind": "records", "label_key": "workTrace.records.looking",
                    "state": "done", "counts": {"found": 1, "read": 1}, "items": [{"ref": ref, "label": label}]}, trace_id="t").to_dict()


def test_a_record_label_that_is_card_text_a_topic_line_or_the_id_is_caught():
    calls = [{"kind": "records", "cards": [{"id": "c1", "used": True}]}]
    for label in ("Latest Python release The conversation's topic: Latest Python release. latest", "Connects the ESOI framework and Quest product intent to the...",
                  "User: what is the latest python", "c1"):
        assert any("not a record title" in p for p in check_turn(calls, [records_titled(label)])), label
    assert check_turn(calls, [records_titled("Run a marathon in April")]) == []


def test_a_record_the_reply_does_not_draw_on_is_caught_when_the_reply_says_it_found_nothing():
    calls = [{"kind": "records", "cards": [{"id": "c1", "used": True, "text": "Connects the ESOI framework and Quest product intent"}]},
             {"kind": "plan_facts", "facts": ["card c1 connects the ESOI framework and Quest product intent to alternatives"]}]
    nothing = "I could not find any logged runs, so I cannot say how the marathon is going."
    assert any("does not draw on" in p for p in check_turn(calls, [records_titled("ESOI framework")], reply=nothing))
    drew = "Your ESOI framework quest connects the ESOI framework and Quest product intent, so start there."
    assert check_turn(calls, [records_titled("ESOI framework")], reply=drew) == []


# --- shared thread: a record every participant may see is named; every other title is a leak -----------------------

TEAM_REC = {"ref": "quest-team", "label": "Launch plan", "kind": "quest", "visible_to_all": True}
PRIVATE_REC = {"ref": "quest-mine", "label": "My secret goal", "kind": "quest", "visible_to_all": False}


def records_events(items):
    return [project({"step_id": "records", "solution_id": "context-assembler", "kind": "records", "label_key": "workTrace.records.looking",
                     "state": "done", "counts": {"found": len(items), "read": len(items)}, "items": items}, trace_id="t1").to_dict()]


RECORD_CALLS = [{"kind": "records", "cards": [{"id": "quest-team", "title": "Launch plan", "text": "", "used": True},
                                              {"id": "quest-mine", "title": "My secret goal", "text": "", "used": True}]}]


def test_a_shared_thread_may_name_a_record_marked_visible_to_all_and_nothing_else():
    named = records_events([TEAM_REC])
    assert check_turn(RECORD_CALLS[:1], named, shared=True, private_titles=["My secret goal"]) == []
    # the hub's projector for a viewer who may not see titles keeps only the marked one
    others = [project(records_events([TEAM_REC, PRIVATE_REC])[0], viewer_may_see_titles=False).to_dict()]
    assert [i["label"] for i in others[0]["items"]] == ["Launch plan"]
    assert check_turn(RECORD_CALLS, others, shared=True, private_titles=["My secret goal"]) == []


def test_a_private_title_in_a_shared_thread_is_a_leak_even_when_other_records_are_marked():
    leaked = records_events([TEAM_REC, PRIVATE_REC])           # the asker's view, unredacted, was sent to everyone
    problems = check_turn(RECORD_CALLS, leaked, shared=True, private_titles=["My secret goal"])
    assert any("private title" in p for p in problems) and any("not visible to everyone" in p for p in problems)
    unmarked = records_events([{"ref": "quest-mine", "label": "My secret goal", "kind": "quest"}])
    assert any("not visible to everyone" in p for p in check_turn(RECORD_CALLS, unmarked, shared=True))


def link_turn(url, verdict="DEAD"):
    trace_sink = Sink()
    trace = work_trace.start(trace_sink, {"work_trace": {"audience": "owner"}})
    trace.links_checked([{"url": url, "verdict": verdict}, {"url": "https://fine.example.org/page", "verdict": "OK"}])
    return [{"kind": "links", "verdicts": [{"url": url, "verdict": verdict}, {"url": "https://fine.example.org/page", "verdict": "OK"}]}], trace_sink.events


def test_a_link_step_names_only_the_domain_of_a_link_left_out():
    calls, events = link_turn("https://www.example.com/private/path?token=abc123#part")
    assert check_turn(calls, events) == []
    data = events[-1].data
    assert [i["ref"] for i in data["items"]] == ["example.com"]


def test_a_url_path_or_query_in_a_link_step_is_caught():
    calls, events = link_turn("https://www.example.com/private/path?token=abc123")
    bad = dict(events[-1].data, items=[{"ref": "example.com", "label": "example.com/private/path", "kind": "removed"}])
    problems = check_turn(calls, [bad])
    assert any("not a bare domain" in p for p in problems)
    bad2 = dict(events[-1].data, items=[{"ref": "example.com", "label": "example.com", "kind": "removed"}], affects_user=True)
    bad2["items"][0]["sub"] = "private/path"
    assert any("path, query or fragment" in p for p in check_turn(calls, [bad2]))
    wrong = dict(events[-1].data, items=[{"ref": "other.example.net", "label": "other.example.net", "kind": "removed"}])
    assert any("no removed link has" in p for p in check_turn(calls, [wrong]))
    ok_link = dict(events[-1].data, counts={"checked": 2, "failed": 0}, items=[{"ref": "fine.example.org", "label": "fine.example.org", "kind": "removed"}])
    assert any("none was left out" in p for p in check_turn(calls, [ok_link]))
