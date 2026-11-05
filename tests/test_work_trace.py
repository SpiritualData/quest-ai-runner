"""The turn's work trace: events derived from structural results, the privacy projection, and the seam wiring.

Offline: fake web, scholarly and link-guard results, no network and no model."""
import json
from types import SimpleNamespace

from hubofhubs.the_world.tools.deep import ProgressEvent
from hubofhubs.the_world.tools.events import EVENT_WORK_TRACE
from hubofhubs.the_world.observation import Observation
from hubofhubs.the_consciousness.terminal.trace import validate

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


def test_a_second_search_does_not_count_the_same_paper_twice():
    """Found by the real run of 2026-11-01: three searches summed to 514 found and 15 read from overlapping results."""
    sink, trace = make()
    first = [{"title": "A", "authors": ["Ana Lee"], "year": 2020, "doi": "10.1/a"}, {"title": "B", "authors": [], "year": 2021, "doi": "10.1/b"}]
    second = [{"title": "B", "authors": [], "year": 2021, "doi": "10.1/b"}, {"title": "C", "authors": [], "year": 2022, "doi": "10.1/c"}]
    for works, total in ((first, 207), (second, 294)):
        trace.papers_started()
        trace.papers_searched(ToolResult(ok=True, text="T", data={"works": works, "total": total}))
    step = sink.latest()["papers"]
    assert step["counts"] == {"found": 294, "read": 3} and len(step["items"]) == 3


def test_one_paper_under_two_dois_is_read_once():
    sink, trace = make()
    trace.papers_started()
    works = [{"title": "Training Load and Knee Pain", "authors": [], "year": 2020, "doi": "10.1/preprint"},
             {"title": "TRAINING LOAD AND KNEE PAIN", "authors": [], "year": 2020, "doi": "10.1/journal"}]
    trace.papers_searched(ToolResult(ok=True, text="T", data={"works": works, "total": 2}))
    assert sink.latest()["papers"]["counts"] == {"found": 2, "read": 1}


def test_web_items_say_which_pages_were_opened_and_which_a_search_only_listed():
    sink, trace = make()
    trace.web_started()
    trace.web_searched(Observation(kind="query", hits=[{"title": "A", "url": "https://a.example/x"}, {"title": "B", "url": "https://b.example/y"}]))
    trace.web_started()
    trace.web_page_read("https://a.example/x", Observation(kind="page", text="p"))
    kinds = {i["ref"]: i["kind"] for i in sink.latest()["web"]["items"]}
    assert kinds == {"a.example": "read", "b.example": "found"}


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
    trace.finish("Your Run a marathon quest and Morning pages both say the same.")
    step = sink.latest()["records"]
    assert step["counts"] == {"found": 2, "read": 2}
    assert [(i["label"], i.get("kind")) for i in step["items"]] == [("Run a marathon", "quest"), ("Morning pages", "reflection")]


def test_records_are_counts_only_in_a_shared_thread_or_unknown_audience():
    for audience in ("shared", "", "someone"):
        sink, trace = make(audience)
        trace.records_used(CARDS)
        trace.finish("Your Run a marathon quest and Morning pages both say the same.")
        step = sink.latest()["records"]
        assert step["items"] == [] and step["counts"]["found"] == 2
        assert "marathon" not in json.dumps([e.data for e in sink.events])


def test_link_check_counts_only_and_affects_user_when_one_was_removed():
    sink, trace = make()
    trace.links_checked([{"url": "https://a.example.org/p?token=SECRET", "verdict": "OK"},
                         {"url": "https://b.example.org/p", "verdict": "DEAD", "reason": "404"}])
    step = sink.latest()["links"]
    assert step["kind"] == "check" and step["counts"] == {"checked": 2, "failed": 1} and step["affects_user"] is True
    # Only the domain of the link that was left out leaves: no path, no query, nothing about the link that checked out.
    assert step["items"] == [{"ref": "b.example.org", "label": "b.example.org", "kind": "removed"}]
    assert "SECRET" not in json.dumps(step) and "/p" not in json.dumps(step) and "a.example.org" not in json.dumps(step)
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


FULL_CARDS = [
    {"id": "marathon-april-quest", "title": "Run a marathon", "adapter": "quest", "item_type_counts": {"quest": 1},
     "rendered_section": "Run a marathon in April: week 6 of 16, left knee sore after hills, physio visit"},
    {"id": "candle-shop-launch", "title": "Candle shop", "adapter": "quest", "item_type_counts": {"quest": 1},
     "rendered_section": "Launch hand-poured candles shop: wax shipment slipped, soft launch 3 Nov"},
]


def test_selected_cards_alone_show_no_records_step():
    sink, trace = make()
    trace.records_selected(CARDS, FULL_CARDS)
    trace.facts_noted(["Python 3.14 is the latest stable release"])
    trace.finish("Python 3.14 is out.")
    assert "records" not in sink.latest()


def test_a_record_the_plan_quoted_is_the_only_one_counted():
    sink, trace = make()
    light = [{"id": c["id"], "title": c["title"], "adapter": "quest", "item_type_counts": {"quest": 1}} for c in FULL_CARDS]
    trace.records_selected(light, FULL_CARDS)
    trace.facts_noted(['"left knee sore after hills" (your marathon quest)'])
    trace.finish("Your left knee is sore after hills, so swap one hill session for a swim.")
    step = sink.latest()["records"]
    assert step["counts"] == {"found": 1, "read": 1}
    assert [i["ref"] for i in step["items"]] == ["marathon-april-quest"]


def test_a_card_named_by_id_or_title_in_a_fact_counts_and_a_short_name_does_not():
    sink, trace = make()
    light = [{"id": "c1", "title": "Run", "adapter": "quest"}, {"id": "candle-shop-launch", "title": "Candle shop", "adapter": "quest"}]
    trace.records_selected(light, [])
    trace.facts_noted(["Run every day", "see candle-shop-launch for the budget"])
    trace.finish("The candle-shop-launch budget is the open question; Candle shop is on track.")
    assert [i["ref"] for i in sink.latest()["records"]["items"]] == ["candle-shop-launch"]


def test_a_card_fetched_by_id_counts():
    sink, trace = make()
    trace.records_selected(CARDS, [])
    trace.records_used([{"id": "c2"}])
    trace.finish("Morning pages is the habit to protect on travel days.")
    assert sink.latest()["records"]["counts"] == {"found": 1, "read": 1}


def test_shared_thread_records_stay_counts_only_when_used():
    sink, trace = make("shared")
    light = [{"id": c["id"], "title": c["title"], "adapter": "quest"} for c in FULL_CARDS]
    trace.records_selected(light, FULL_CARDS)
    trace.facts_noted(["left knee sore after hills"])
    trace.finish("A left knee sore after hills calls for rest.")
    step = sink.latest()["records"]
    assert step["items"] == [] and step["counts"]["read"] == 1


WORKS = [{"title": "Training Load and Knee Pain in Marathoners", "authors": ["Ana Lee", "B Ode"], "year": 2021, "doi": "10.1/a"},
         {"title": "Hill Running Biomechanics Review", "authors": ["Cho Park"], "year": 2019, "doi": "10.1/b"},
         {"title": "Unrelated Cartilage Imaging Study", "authors": ["Dee Roe", "E Fox", "G Hu"], "year": 2018, "doi": "10.1/c"}]


def test_papers_the_reply_cites_are_read_and_the_rest_are_only_found():
    sink, trace = make()
    trace.papers_started()
    trace.papers_searched(ToolResult(ok=True, text="T", data={"works": WORKS, "total": 31}))
    assert sink.latest()["papers"]["counts"] == {"found": 31, "read": 3}      # live: the abstracts the run took
    trace.finish("Load matters (Lee and Ode, 2021). Another review is at doi 10.1/b. Nothing else applies.")
    step = sink.latest()["papers"]
    assert step["counts"] == {"found": 31, "read": 2}
    assert [i["ref"] for i in step["items"]] == ["10.1/a", "10.1/b"]


def test_a_paper_cited_by_full_title_counts_and_none_cited_leaves_found_only():
    sink, trace = make()
    trace.papers_started()
    trace.papers_searched(ToolResult(ok=True, text="T", data={"works": WORKS, "total": 3}))
    trace.finish("See Unrelated Cartilage Imaging Study for more.")
    assert [i["ref"] for i in sink.latest()["papers"]["items"]] == ["10.1/c"]
    sink2, trace2 = make()
    trace2.papers_started()
    trace2.papers_searched(ToolResult(ok=True, text="T", data={"works": WORKS, "total": 3}))
    trace2.finish("There is research, but I will not cite it.")
    step = sink2.latest()["papers"]
    assert step["counts"] == {"found": 3, "read": 0} and step["items"] == []


def test_a_listed_site_the_reply_never_cites_is_not_shown_but_still_counted_as_found():
    sink, trace = make()
    trace.web_started()
    trace.web_searched(Observation(kind="query", hits=[{"title": "A", "url": "https://a.example/x"}, {"title": "B", "url": "https://b.example/y"},
                                                       {"title": "C", "url": "https://c.example/z"}]))
    trace.finish("The release is on a.example, per its announcement.")
    step = sink.latest()["web"]
    assert step["counts"] == {"found": 3, "read": 0}
    assert [i["ref"] for i in step["items"]] == ["a.example"]


def test_a_record_label_is_a_human_title_never_card_text_or_a_conversation_line():
    sink, trace = make()
    cards = [{"id": "q1", "title": "Run a marathon in April", "adapter": "quest"},
             {"id": "q2", "title": "Latest Python release The conversation's topic: Latest Python release. latest stable", "adapter": "quest"},
             {"id": "q3", "title": "Connects the ESOI framework and Quest product intent to the competitive alter...", "adapter": "quest"},
             {"id": "q4", "title": "User: what is the latest python", "adapter": "quest"},
             {"id": "q5", "title": "", "adapter": "quest"},
             {"id": "q6-slug", "title": "q6 slug", "adapter": "quest"}]
    trace.records_used(cards)
    trace.finish("Run a marathon in April, q6 slug, Latest Python release, Connects the ESOI framework.")
    step = sink.latest()["records"]
    assert [i["label"] for i in step["items"]] == ["Run a marathon in April"] and step["counts"] == {"found": 1, "read": 1}


FIND_NOTHING = {"id": "esoi-framework", "title": "ESOI framework", "adapter": "quest", "item_type_counts": {"note": 1},
                "rendered_section": "Connects the ESOI framework and Quest product intent to the competitive alternatives"}


def test_a_reply_that_found_nothing_in_a_record_it_read_does_not_carry_it():
    """The planner took a fact from the card and the run fetched it, but the reply says nothing was found: the record is not shown."""
    sink, trace = make()
    trace.records_selected([FIND_NOTHING], [FIND_NOTHING])
    trace.facts_noted(["the card connects the ESOI framework and Quest product intent to the competitive alternatives"])
    trace.records_used([FIND_NOTHING], [FIND_NOTHING])
    trace.finish("I could not find any logged runs for you, so I cannot say how your marathon training is going. Tell me what you ran.")
    assert "records" not in sink.latest()


def test_a_reply_that_says_it_has_no_web_search_carries_no_records_and_no_sites():
    sink, trace = make("shared")
    team = [{"id": f"t{i}", "title": f"Team quest {i}", "adapter": "quest", "item_type_counts": {"quest": 1},
             "rendered_section": f"Team quest {i} tracks the work item number {i} for the launch plan"} for i in range(7)]
    trace.records_selected(team, team)
    trace.facts_noted([f"Team quest {i} tracks the work item number {i} for the launch plan" for i in range(7)])
    trace.web_started()
    trace.web_searched(Observation(kind="query", hits=[{"title": "A", "url": "https://medium.com/x"}, {"title": "B", "url": "https://blog.example.org/y"}]))
    trace.finish("I have no working web search here, so I cannot check the latest Go release. I can tell you what I knew when I was trained.")
    latest = sink.latest()
    assert "records" not in latest
    assert latest["web"]["items"] == [] and latest["web"]["counts"] == {"found": 2, "read": 0}      # searched, no page opened


def test_only_the_exact_site_the_reply_names_is_cited():
    assert work_trace.host_cited("blog.rust-lang.org", "see https://blog.rust-lang.org/2026/x")
    assert work_trace.host_cited("python.org", "see https://www.python.org/downloads/ for it")
    assert not work_trace.host_cited("docs.python.org", "see https://www.python.org/downloads/ for it")
    assert not work_trace.host_cited("blog.rust-lang.org", "see rust-lang.org for the notes")
    assert not work_trace.host_cited("medium.com", "the platform is not named medium dot com here")
    assert not work_trace.host_cited("python.org", "see notpython.org or python.org.evil.example")


def test_a_vector_hit_is_titled_by_its_own_name_not_by_the_slice_of_its_text():
    from quest_ai_runner.core.orchestrator_events import _project_card_metadata_for_event
    hit = {"id": "card:m1", "title": "Run a marathon in April Week 6 of 16. Left knee sore after hills, physio visit booked for Thursday.",
           "name": "Run a marathon in April", "adapter": "vector"}
    bodyless = {"id": "card:m2", "title": "Morning pages habit Skipping pages on travel days. Goal is 20 days in a row; streak 4.", "adapter": "vector"}
    light = _project_card_metadata_for_event([hit, bodyless])
    assert light[0]["name"] == "Run a marathon in April" and "name" not in light[1]
    sink, trace = make()
    trace.records_used(light)
    trace.finish("Run a marathon in April is the quest to protect, and Morning pages habit can wait.")
    assert [i["label"] for i in sink.latest()["records"]["items"]] == ["Run a marathon in April"]


def test_link_check_is_sent_running_before_it_finishes_and_closed_when_nothing_was_checked():
    sink, trace = make()
    trace.links_checking()
    assert sink.latest()["links"]["state"] == "running"
    trace.links_checked([{"url": "https://a.example.org/p", "verdict": "OK"}])
    assert sink.latest()["links"]["state"] == "done"
    sink2, trace2 = make()
    trace2.links_checking()
    trace2.links_checked([])
    assert sink2.latest()["links"]["state"] == "done" and sink2.latest()["links"]["items"] == []


def test_each_removed_link_is_named_by_its_domain_only_and_a_clean_check_names_none():
    sink, trace = make()
    trace.links_checked([{"url": "https://www.one.example.org/a/b?token=SECRET#frag", "verdict": "DEAD"},
                         {"url": "http://user:pw@two.example.org:8080/x", "verdict": "UNKNOWN"},
                         {"url": "doi:10.1/x", "verdict": "UNKNOWN"},
                         {"url": "https://ok.example.org/fine", "verdict": "OK"}])
    step = sink.latest()["links"]
    assert step["counts"] == {"checked": 4, "failed": 3}
    assert [i["ref"] for i in step["items"]] == ["one.example.org", "two.example.org"]   # a link with no domain is counted, not named
    blob = json.dumps(step)
    assert not any(bit in blob for bit in ("SECRET", "frag", "pw", "8080", "/a/b", "ok.example.org"))
    clean, trace2 = make()
    trace2.links_checked([{"url": "https://ok.example.org/fine", "verdict": "OK"}])
    assert clean.latest()["links"]["items"] == [] and clean.latest()["links"]["affects_user"] is False


# The saved turn where a spaced-repetition question about language-learning papers was wrongly shown "Used your record: Conversational Spanish":
# the card and the reply shared only the common words "practice" and "second".
SPANISH_CARD = {"title": "Conversational Spanish", "text": "25 minutes of daily practice, stuck on past tenses. Tutor session every second Tuesday."}
SPANISH_FACT = 'spanish-practice card: "25 minutes of daily practice, stuck on past tenses. Tutor session every second Tuesday."'
SPACING_REPLY = (
    "I found no meta-analysis of spaced repetition itself for second-language learning. "
    "- Nakata (2015), \"Effects of Expanding and Equal Spacing on Second Language Vocabulary Learning\". "
    "- Nakata (2016), \"Does Repeated Practice Make Perfect? The Effects of Within-Session Repeated Retrieval on Second Language Vocabulary Learning\". "
    "This concerns repetition within a session, not spacing across days.")


def test_common_words_shared_with_a_card_do_not_make_the_reply_draw_on_it():
    assert not work_trace.reply_draws_on(SPANISH_CARD, [SPANISH_FACT], SPACING_REPLY)
    assert work_trace.distinctive_words(SPACING_REPLY) & work_trace.distinctive_words(SPANISH_CARD["text"]) == set()


def test_a_reply_that_really_uses_the_record_still_draws_on_it():
    marathon = {"title": "Run a marathon in April",
                "text": "Week 6 of 16. Left knee sore after hills, physio visit booked for Thursday. Swimming twice a week. Long run of 24 km planned for Sunday."}
    # three rare words or numbers shared (knee, physio, 24)
    assert work_trace.reply_draws_on(marathon, [], "Rest the knee until the physio visit; the 24 long run can wait.")
    # the title's rare words stand in the reply
    assert work_trace.reply_draws_on(marathon, [], "Your marathon plan is on track; April is far off.")
    # a verbatim run of the card's words
    assert work_trace.reply_draws_on(SPANISH_CARD, [], "You said you are stuck on past tenses, so start there.")
    assert work_trace.reply_draws_on(SPANISH_CARD, [], "Your conversational Spanish practice is the one slipping.")
    # but two rare words alone, or common words however many, are not enough
    assert not work_trace.reply_draws_on(marathon, [], "A knee needs rest and a physio.")
    assert not work_trace.reply_draws_on(SPANISH_CARD, [], "Daily practice every second day with a tutor session minutes long.")
    # one word of a two-word title is not the title
    assert not work_trace.reply_draws_on(SPANISH_CARD, [], "Learning Spanish takes time.")
    # numbers of one digit are not distinctive
    assert not work_trace.reply_draws_on({"title": "Plan", "text": "Week 6 of 7 weeks, then 8 more."}, [], "There are 6 steps, 7 notes and 8 items.")


def test_without_a_registered_word_table_only_a_title_or_a_quote_can_show_a_record():
    registered = work_trace._rare_word
    try:
        work_trace.set_word_rarity(lambda word: False)
        assert not work_trace.reply_draws_on(SPANISH_CARD, [], "Daily practice, tenses, tutor, Tuesday, minutes.")
        assert work_trace.reply_draws_on(SPANISH_CARD, [], "Your conversational spanish goal is slipping.")
    finally:
        work_trace.set_word_rarity(registered)


def test_a_fact_that_names_several_cards_is_not_a_quote_of_any_of_them():
    cards = {"candle-shop-launch": {"title": "Launch the candle shop", "text": ""}, "morning-pages-habit": {"title": "Morning pages habit", "text": ""}}
    remark = "Vector hit candle-shop-launch and morning-pages-habit are WEAK subject matches, so they are not used."
    own = "[morning-pages-habit] Morning pages habit: Skipping pages on travel days."
    assert work_trace.facts_about("morning-pages-habit", [remark, own], cards) == [own]
    assert work_trace.facts_about("candle-shop-launch", [remark, own], cards) == []
    sink, trace = make()
    light = [{"id": cid, "title": c["title"], "adapter": "quest"} for cid, c in cards.items()]
    trace.records_selected(light, [])
    trace.facts_noted([remark])
    trace.finish("Skipping a few pages on travel days is fine; the vector hits were weak matches.")
    assert "records" not in sink.latest()


SPANISH = {"id": "spanish-practice", "title": "Conversational Spanish", "name": "Conversational Spanish", "adapter": "quest",
           "item_type_counts": {"note": 1}, "rendered_section": "25 minutes of daily practice, stuck on past tenses. Tutor session every second Tuesday."}
SPANISH_ALIAS = dict(SPANISH, id="card:spanish-practice", item_type_counts={})
SPANISH_FACTS = ["spanish-practice card note: \"25 minutes of daily practice, stuck on past tenses. Tutor session every second Tuesday.\"",
                 "\"Conversational Spanish: 25 minutes of daily practice, stuck on past tenses. Tutor session every second Tuesday.\" (card spanish-practice)"]
SPANISH_REPLY = ("Your Spanish practice is set up for 25 minutes a day, with a tutor session every second Tuesday, "
                 "and your note says you're stuck on past tenses.")


def test_one_card_under_two_ids_is_one_record():
    """The same card reached the trace as ``spanish-practice`` and ``card:spanish-practice``: one record, counted once, and used (a paraphrase)."""
    sink, trace = make()
    trace.records_selected([SPANISH, SPANISH_ALIAS], [SPANISH, SPANISH_ALIAS])
    trace.facts_noted(SPANISH_FACTS)
    trace.finish(SPANISH_REPLY)
    step = sink.latest()["records"]
    assert step["counts"] == {"found": 1, "read": 1}
    assert [i["label"] for i in step["items"]] == ["Conversational Spanish"]


def test_one_card_under_two_ids_stays_counts_only_in_a_shared_thread():
    sink, trace = make("shared")
    trace.records_selected([SPANISH, SPANISH_ALIAS], [SPANISH, SPANISH_ALIAS])
    trace.facts_noted(SPANISH_FACTS)
    trace.finish(SPANISH_REPLY)
    step = sink.latest()["records"]
    assert step["counts"] == {"found": 1, "read": 1} and step["items"] == []


def test_facts_about_a_card_known_by_two_ids_are_not_ambiguous():
    cards = {c["id"]: {"title": c["title"], "text": c["rendered_section"]} for c in (SPANISH, SPANISH_ALIAS)}
    assert work_trace.facts_about("spanish-practice", SPANISH_FACTS, cards) == SPANISH_FACTS


MORNING_PAGES = {"id": "morning-pages-habit", "title": "Morning pages habit", "adapter": "quest"}
MORNING_TEXT = "Skipping pages on travel days. Goal is 20 days in a row; current streak is 4 days."
MORNING_FACTS = ['morning-pages-habit card: "' + MORNING_TEXT + '"']
MORNING_REPLY = ("Your morning pages streak is 4 days, against a goal of 20 days in a row. "
                 "The habit card notes that skipping pages on travel days is what breaks it.")


def test_a_title_that_reads_like_its_slug_is_still_the_title_a_person_gave():
    """Held-out2 records turn (saved capture): the reply paraphrased the card and no record was shown, because the title of a card whose id is the
    slugged title ("Morning pages habit" / ``morning-pages-habit``) was treated as the id leaking."""
    assert work_trace.is_record_title("Morning pages habit", "morning-pages-habit")
    assert not work_trace.is_record_title("morning-pages-habit", "morning-pages-habit")
    assert not work_trace.is_record_title("marathon", "marathon")
    sink, trace = make()
    trace.records_selected([MORNING_PAGES], [dict(MORNING_PAGES, rendered_section=MORNING_TEXT)])
    trace.facts_noted(MORNING_FACTS)
    trace.finish(MORNING_REPLY)
    assert sink.latest()["records"]["items"][0]["label"] == "Morning pages habit"


def test_a_slug_titled_card_the_reply_leaves_out_still_does_not_count():
    sink, trace = make()
    trace.records_selected([MORNING_PAGES], [dict(MORNING_PAGES, rendered_section=MORNING_TEXT)])
    trace.facts_noted(MORNING_FACTS)
    trace.finish("A good name for your sourdough starter is Bubbles.")
    assert "records" not in sink.latest()


def test_a_slug_titled_card_stays_counts_only_for_other_members():
    sink, trace = make(audience="shared")
    trace.records_selected([MORNING_PAGES], [dict(MORNING_PAGES, rendered_section=MORNING_TEXT)])
    trace.facts_noted(MORNING_FACTS)
    trace.finish(MORNING_REPLY)
    step = sink.latest()["records"]
    assert "Morning pages" not in json.dumps(step)
    assert step["counts"]["read"] == 1


def test_answering_step_runs_after_work_and_is_closed_by_the_draft_the_link_check_or_the_end():
    sink, trace = make()
    trace.web_started()
    trace.answering()
    assert sink.latest()["answer"]["state"] == "running" and sink.latest()["answer"]["kind"] == "answer"
    trace.answered()
    assert sink.latest()["answer"]["state"] == "done"
    trace.answered()    # closing twice is harmless
    sink2, trace2 = make()
    trace2.web_started()
    trace2.answering()
    trace2.links_checking()
    assert sink2.latest()["answer"]["state"] == "done" and sink2.latest()["links"]["state"] == "running"
    sink3, trace3 = make()
    trace3.web_started()
    trace3.answering()
    trace3.finish("")    # an ended or failed turn never leaves it running
    assert sink3.latest()["answer"]["state"] == "done"


def test_answering_step_is_not_sent_for_an_instant_answer_with_no_prior_work():
    sink, trace = make()
    trace.answering()
    trace.finish("hello")
    assert "answer" not in sink.latest()


def test_the_answering_step_changes_no_counts_or_items_of_the_other_steps():
    sink, trace = make()
    trace.web_started()
    trace.web_searched(Observation(kind="query", hits=[{"title": "A", "url": "https://a.example.org/p", "snippet": "s", "query": "q"}]))
    before = {k: v for k, v in sink.latest().items() if k != "answer"}
    trace.answering()
    trace.finish("")
    after = {k: v for k, v in sink.latest().items() if k != "answer"}
    for step in ("web",):
        assert before[step]["counts"] == after[step]["counts"]
