"""Offline tests for end_to_end: every case names its source, the scripted arm passes every case through the real orchestrator,
the no-model floor passes some and fails the rest, and the scorer separates all of them from the deliberately broken baseline.
No model call, no network. Run: .venv/bin/python3 -m pytest evaluation/components/test_end_to_end.py -q -p no:cacheprovider"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
from end_to_end import score, world  # noqa: E402

CASES = json.loads((HERE / "end_to_end" / "cases.json").read_text())
BY_ID = {c["id"]: c for c in CASES}


def arm(name):
    return common.load_impl(name, "end_to_end")(common.CallMeter(max_calls=600))


def passed(fn):
    return {c["id"]: score.score_case(fn, c)["ok"] for c in CASES}


def test_every_case_names_its_source_and_the_set_spans_the_kinds():
    assert len(BY_ID) == len(CASES) >= 12
    assert all(len(c["source"]) > 30 for c in CASES)
    assert {c["kind"] for c in CASES} >= {"own_data", "note_write", "goal_write", "multistep", "implicit_quest", "web",
                                          "hand_off", "terminal", "refusal", "web_goal", "quest_consult", "quest_clarify", "quest_why", "quest_feature", "quest_mentor",
                                          "quest_settings", "quest_trap", "quest_ai", "docs_lookup", "science", "sequential"}


def test_the_scripted_arm_passes_every_case_through_the_real_orchestrator():
    results = passed(arm("reference_script"))
    assert all(results.values()), [k for k, v in results.items() if not v]


def test_the_floor_passes_some_cases_and_fails_every_question_about_the_account():
    results = passed(arm("rules_only"))
    assert 4 <= sum(results.values()) < len(CASES)
    assert not results["E2E-OWN-01"] and not results["E2E-WEB-01"] and not results["E2E-IMPL-01"]


def test_the_broken_baseline_fails_what_the_cases_label():
    broken = common.load_impl("end_to_end.broken.act_on_everything:build", "end_to_end")(common.CallMeter())
    results = passed(broken)
    assert sum(results.values()) <= 2
    assert sum(score.score_case(broken, c)["unrequested"] for c in CASES) >= 8


def test_a_write_on_the_wrong_quest_or_an_extra_write_fails():
    case = BY_ID["E2E-IMPL-01"]
    good = {"reply": "ok", "route": "act", "web": [], "reads": [],
            "writes": [{"tool": "create_quest_goal", "args": {"title": "Call the plumber"}, "quest_id": "q_home"}]}
    assert score.score_case(lambda c: good, case)["ok"]
    wrong = dict(good, writes=[dict(good["writes"][0], quest_id="q_fit")])
    assert "landed on q_fit" in score.score_case(lambda c: wrong, case)["note"]
    extra = dict(good, writes=good["writes"] + [{"tool": "add_quest_note", "args": {"text": "x"}, "quest_id": "q_home"}])
    assert "unrequested" in score.score_case(lambda c: extra, case)["note"]


def test_an_invented_figure_for_an_absent_fact_fails_the_grounding_case():
    case = BY_ID["E2E-OWN-02"]
    out = {"reply": "Your revenue to date is $1,200 from 40 orders.", "route": "answer", "writes": [], "reads": [], "web": []}
    assert not score.score_case(lambda c: out, case)["ok"]


def test_the_world_records_a_write_on_the_pinned_quest_and_reads_do_not_count():
    w = world.World(pinned="q_fit")
    w.registry.get("add_quest_note").handler({"text": "n"}, None)
    w.registry.get("list_quest_notes").handler({}, None)
    assert [x["quest_id"] for x in w.writes] == ["q_fit"] and [r["tool"] for r in w.reads] == ["list_quest_notes"]


# ---- the cases about Quest itself, the web-then-goal case and the added action cases

QUEST_CASES = [c for c in CASES if c["id"].startswith("E2E-QUEST-")]
TEXT_BEARING = [c for c in CASES if "quality_rubric" in c]
NO_DASH = "\u2014", "\u2013"


def reply_ok(case_id, reply, route="answer"):
    case = BY_ID[case_id]
    out = {"reply": reply, "route": route, "writes": [], "reads": [], "web": []}
    return score.score_case(lambda c: out, case)


def test_the_quest_questions_cover_the_groups_and_each_carries_a_rubric_and_provenance():
    assert len(QUEST_CASES) >= 24
    assert all(3 <= len(c["quality_rubric"]) <= 5 for c in TEXT_BEARING)
    assert {c.get("side") for c in QUEST_CASES} >= {"mentee", "mentor"}
    assert {c["group"] for c in CASES if "group" in c} == {"Questions about Quest", "Settings and account", "Multi-step operations"}
    spec = world.SPEC
    assert spec["docs_provenance"]["copied_from"] == "quest-docs" and spec["docs_provenance"]["commit"]
    for c in TEXT_BEARING:
        assert all(d in spec["docs"] for d in c["world"].get("docs", [])), c["id"]
        assert not any(ch in json.dumps(c, ensure_ascii=False) for ch in NO_DASH), c["id"]
        assert "free to start" not in json.dumps(c["ideal"]).lower()


def test_the_documentation_excerpts_hold_no_link_name_or_address():
    for key, d in world.SPEC["docs"].items():
        assert d["source"].startswith("docs/") and d["text"].strip(), key
        assert not re.search(r"https?://|@[\w-]+\.", d["text"]), key


def test_a_vague_goal_must_be_met_with_a_question_and_no_assumed_circumstance():
    good = "Yes, it can help with a goal. Which part of your life would you most like to change this year, and what have you tried before?"
    assert reply_ok("E2E-QUEST-02", good)["ok"]
    assert "asks no question" in reply_ok("E2E-QUEST-02", "Yes, Quest can help you with your goals and your plan for the year.")["note"]
    assert not reply_ok("E2E-QUEST-02", "Since you work long hours, a plan helps. What is your goal?")["ok"]
    assert "too long" in reply_ok("E2E-QUEST-02", "A goal plan. " * 100 + "Which goal?")["note"]


def test_a_reply_that_invents_a_plan_a_price_a_dash_or_free_to_start_fails():
    ideal = BY_ID["E2E-QUEST-02"]["ideal"]["reply"]
    assert reply_ok("E2E-QUEST-02", ideal)["ok"]
    assert not reply_ok("E2E-QUEST-02", ideal + " It is free to start.")["ok"]
    assert not reply_ok("E2E-QUEST-02", ideal + " Quest Pro costs $25 a month.")["ok"]
    assert not reply_ok("E2E-QUEST-02", ideal.replace(", for example", " \u2014 for example"))["ok"]
    pricing = BY_ID["E2E-QUEST-02"]
    assert reply_ok("E2E-QUEST-02", ideal)["ok"] and pricing["kind"] == "quest_clarify"


def test_the_pricing_rule_free_forever_thirty_one_days_and_data_never_lost():
    case = "E2E-QUEST-01"
    ideal = BY_ID["E2E-QUEST-24"]["ideal"]["reply"]
    assert reply_ok("E2E-QUEST-24", ideal)["ok"]  # the wearable trap carries no price at all
    assert not reply_ok("E2E-QUEST-24", ideal + " It costs $15 a month.")["ok"]
    assert case in BY_ID


def test_a_trap_passes_only_on_not_sure_and_fails_on_an_invented_feature_or_a_flat_denial():
    for trap in ("E2E-QUEST-12", "E2E-QUEST-24", "E2E-QUEST-25"):
        assert reply_ok(trap, BY_ID[trap]["ideal"]["reply"])["ok"], trap
    assert not reply_ok("E2E-QUEST-24", "Yes, Quest syncs with Apple Watch and Fitbit, so your steps count toward your habits.")["ok"]
    assert not reply_ok("E2E-QUEST-24", "Quest does not support Apple Watch or Fitbit, so no, it cannot count steps for you here.")["ok"]
    assert not reply_ok("E2E-QUEST-25", "Yes, SMS reminders are available in Settings.")["ok"]
    assert not reply_ok("E2E-QUEST-12", "Yes, Quest gives a full refund if the mentor does not help you.")["ok"]


def test_the_feature_questions_need_the_documented_facts():
    for cid in ("E2E-QUEST-06", "E2E-QUEST-14", "E2E-QUEST-16", "E2E-QUEST-17", "E2E-QUEST-22"):
        assert reply_ok(cid, BY_ID[cid]["ideal"]["reply"])["ok"], cid
    assert "lacks" in reply_ok("E2E-QUEST-06", "Yes, Quest can track a habit as a number or a time and it is easy to set up for you.")["note"]
    assert not reply_ok("E2E-QUEST-16", "Yes, Quest is available in Spanish, French and German.")["ok"]
    only_ties_one = "Quest has a daily reflection. " * 4
    assert "covers" in reply_ok("E2E-QUEST-01", "Good plan for your half marathon and night shifts. " + only_ties_one)["note"]


def test_the_task_type_cases_promise_only_what_exists():
    chain = BY_ID["E2E-QUEST-21"]["ideal"]["reply"]
    assert reply_ok("E2E-QUEST-21", chain)["ok"]
    assert not reply_ok("E2E-QUEST-21", chain + " You can chain your tasks so each one waits for the previous task to succeed.")["ok"]
    sci = BY_ID["E2E-SCI-01"]["ideal"]["reply"]
    assert reply_ok("E2E-SCI-01", sci)["ok"]
    assert not reply_ok("E2E-SCI-01", "Studies show it helps. See Smith et al. 2019 and Jones and Lee (2021).")["ok"]
    assert not reply_ok("E2E-SCI-01", "Here are two papers: doi: 10.1000/xyz123 and PMID 12345.")["ok"]


def web_goal(reply="Registration closes on 12 Nov 2026. I added a goal to register before then.", web=1, title="Register for the 10K by 12 Nov",
             quest="q_fit"):
    got = {"reply": reply, "route": "act", "web": ["riverside 10k registration"] * web, "reads": [],
           "writes": [{"tool": "create_quest_goal", "args": {"title": title}, "quest_id": quest}] if title else []}
    return score.score_case(lambda c: got, BY_ID["E2E-WEBGOAL-01"])


def test_the_web_then_goal_case_needs_the_search_the_goal_and_the_right_date():
    assert web_goal()["ok"]
    assert "0 web queries" in web_goal(web=0)["note"]
    assert "no create_quest_goal" in web_goal(title="")["note"]
    assert not web_goal(reply="Registration closed last year on 14 Nov 2025.")["ok"]
    assert "landed on q_biz" in web_goal(quest="q_biz")["note"]


def test_the_sequential_case_acts_only_on_the_condition_it_names():
    case = BY_ID["E2E-SEQ-01"]
    got = {"reply": "Total 755 USD, over 700, so I queued a task.", "route": "queue", "web": [], "reads": [],
           "writes": [{"tool": "create_task", "args": {"title": "Cheaper jar suppliers"}, "quest_id": "q_biz"}]}
    assert score.score_case(lambda c: got, case)["ok"]
    extra = dict(got, writes=got["writes"] + [{"tool": "create_quest_goal", "args": {"title": "x"}, "quest_id": "q_biz"}])
    assert "unrequested" in score.score_case(lambda c: extra, case)["note"]


def test_the_quality_rubric_loads_prompts_and_parses_without_a_model():
    from end_to_end import rubric
    case = BY_ID["E2E-QUEST-01"]
    assert len(rubric.items(case)) == 5
    text = rubric.prompt(case, "My reply.", world.World(docs=case["world"]["docs"]).docs)
    assert "My reply." in text and "1. Names the person" in text and "docs/getting-started/index.md" in text
    graded = rubric.parse({"items": [{"n": i, "yes": i <= 3, "why": "ok"} for i in range(1, 6)]}, case)
    assert (graded["passed"], graded["total"]) == (3, 5) and "error" not in graded
    broken = rubric.parse({"error": "unparseable grader reply"}, case)
    assert broken["passed"] == 0 and broken["error"]
    assert rubric.parse({"items": [{"n": 1}]}, case)["passed"] == 0   # a missing yes is not a yes
    try:
        rubric.items({"id": "x", "quality_rubric": ["only one"]})
        raise AssertionError("one item is not a rubric")
    except ValueError:
        pass


def test_the_rubric_grades_a_transcript_directory_with_a_fake_provider(tmp_path):
    from end_to_end import rubric, transcript
    import claude_judge
    case = BY_ID["E2E-QUEST-02"]
    transcript.save(tmp_path, {"case": case["id"], "reply": "Which area matters most?"})
    transcript.save(tmp_path, {"case": "E2E-GOAL-01", "reply": "Added."})
    seen = []
    original = claude_judge.ask_json
    claude_judge.ask_json = lambda provider, prompt: seen.append(prompt) or {"items": [{"n": i, "yes": True} for i in range(1, 6)]}
    try:
        done = rubric.grade_directory(tmp_path, BY_ID, object(), lambda c: [])
    finally:
        claude_judge.ask_json = original
    assert list(done) == ["E2E-QUEST-02"] and len(seen) == 1
    assert json.loads((tmp_path / "E2E-QUEST-02.json").read_text())["rubric"]["passed"] == 5
    assert "rubric" not in json.loads((tmp_path / "E2E-GOAL-01.json").read_text())


# ---- the refresh script that copies the documentation excerpts

def test_the_docs_fixture_refresh_copies_verbatim_and_refuses_links_and_names(tmp_path):
    from end_to_end import refresh_docs_fixture as refresh
    (tmp_path / "a.md").write_text("# Title\n\nFirst para.\n\n## Part\n\nBody line.\n\n## Next\n\nMore.\n", encoding="utf-8")
    spec = {"k": {"path": "a.md", "title": "T", "parts": [{"start": "## Part", "end": "## Next"}, {"start": "First para"}]}}
    got = refresh.excerpts(tmp_path, spec)
    assert got["k"]["text"] == "## Part\n\nBody line.\n\nFirst para." and got["k"]["source"] == "docs/a.md"
    (tmp_path / "b.md").write_text("See https://example.org now\n", encoding="utf-8")
    for bad in ({"k": {"path": "b.md", "title": "T", "parts": [{"start": "See"}]}},
                {"k": {"path": "a.md", "title": "T", "parts": [{"start": "## Missing"}]}}):
        try:
            refresh.excerpts(tmp_path, bad)
            raise AssertionError("must refuse")
        except SystemExit as stop:
            assert "refresh_docs_fixture" in str(stop)
    world_text = (HERE / "end_to_end" / "world.json").read_text(encoding="utf-8")
    again = refresh.render(world_text, world.SPEC["docs"], world.SPEC["docs_provenance"])
    assert json.loads(again) == json.loads(world_text)


def test_the_fixture_matches_the_documentation_checkout_when_one_is_given():
    import os
    import pytest
    from end_to_end import refresh_docs_fixture as refresh
    root = os.environ.get("QUEST_DOCS_DIR")
    if not root:
        pytest.skip("QUEST_DOCS_DIR is not set")
    docs_root = Path(root) / "docs" if (Path(root) / "docs").is_dir() else Path(root)
    assert refresh.excerpts(docs_root) == world.SPEC["docs"]


# ---- the optional transcript

def test_the_transcript_is_off_by_default_and_the_turn_is_unchanged_when_on(tmp_path):
    import scripted_brain
    from end_to_end import drive, transcript
    assert transcript.target_dir({}) is None and transcript.target_dir({transcript.ENV: " "}) is None
    assert transcript.target_dir({transcript.ENV: str(tmp_path)}) == tmp_path

    def run(case, directory):
        w = drive.new_world(case)
        meter = common.CallMeter(max_calls=100)
        script = case["ideal"]
        provider = meter.wrap(drive.IdealPlanner([dict(d) for d in script["decisions"]], script["reply"]))
        orch = scripted_brain.build_brain(provider, tools=w.registry, web=w.web)
        turn = lambda: drive.run_with(case, w, orch)
        got = turn() if directory is None else transcript.record_turn(case, w, orch, meter, turn, directory)
        return got

    for cid in ("E2E-IMPL-01", "E2E-WEBGOAL-01", "E2E-QUEST-02"):
        plain, kept = run(BY_ID[cid], None), run(BY_ID[cid], tmp_path)
        assert plain == kept and score.judge(plain, BY_ID[cid]) == score.judge(kept, BY_ID[cid])
    saved = sorted(p.name for p in tmp_path.iterdir())
    assert saved == ["E2E-IMPL-01.json", "E2E-QUEST-02.json", "E2E-WEBGOAL-01.json"]
    rec = json.loads((tmp_path / "E2E-WEBGOAL-01.json").read_text())
    assert rec["request"] == BY_ID["E2E-WEBGOAL-01"]["message"] and rec["verdict"]["ok"] is True and rec["calls"] > 0
    assert [s["action"] for s in rec["steps"]][:2] == ["read", "tool"]
    assert rec["steps"][0]["did"][0]["type"] == "web search" and rec["steps"][1]["did"][0]["tool"] == "create_quest_goal"
    assert rec["writes"][0]["quest"] == "Sub-50 10K" and rec["pinned_quest"] == "Sub-50 10K"
    impl = json.loads((tmp_path / "E2E-IMPL-01.json").read_text())
    assert impl["pinned_quest"] is None and impl["context_used"][0]["quest"] == "Bathroom renovation"
    quest = json.loads((tmp_path / "E2E-QUEST-02.json").read_text())
    assert quest["docs_in_view"] and not quest["writes"]


def test_a_transcript_step_carries_a_deep_hand_off_and_cuts_long_text():
    from end_to_end import transcript
    step = transcript.decision_step({"action": "deep", "rationale": "x" * 900, "goal": "Research suppliers", "deep_brief": "b" * 900})
    assert step["handoff"]["goal"] == "Research suppliers" and len(step["handoff"]["brief"]) <= 300 and len(step["why"]) <= 200
    assert transcript.cut("a" * 1000, 50).endswith("...") and len(transcript.cut("a" * 1000, 50)) == 50
    w = world.World(pinned="q_biz")
    steps = transcript.build_steps(w, [(0, {"action": "deep", "rationale": "r", "goal": "g", "deep_brief": "b"})])
    assert steps[0]["handoff"] == {"goal": "g", "brief": "b"} and steps[0]["did"] == []


def test_the_live_arm_reads_the_flag_and_nothing_else_changes():
    src = (HERE / "end_to_end" / "impls" / "qar_live.py").read_text()
    assert "transcript.target_dir()" in src and "return drive.run_with(case, world, orch)" in src
