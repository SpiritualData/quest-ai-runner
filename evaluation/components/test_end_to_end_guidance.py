"""Offline tests for the guidance cases of end_to_end (cases_guidance.json: written from the accountability meeting of 2026-10-27).
No model call and no network: the scorer is run on hand-written good and bad turn records, so a case that could never fail, or one
that fails a right reply, is caught here. The live run is ``end_to_end/guidance_live.py`` (a cheap model, on the staged ladder).
Run: .venv/bin/python3 -m pytest evaluation/components/test_end_to_end_guidance.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
from end_to_end import drive, score, world  # noqa: E402

CASES = json.loads((HERE / "end_to_end" / "cases_guidance.json").read_text(encoding="utf-8"))
BY_ID = {c["id"]: c for c in CASES}


def rec(route="answer", reply="", writes=()):
    return dict(reply=reply, writes=list(writes), reads=[], web=[], asks=[], route=route)


def goal(title):
    return dict(tool="create_quest_goal", args={"title": title}, quest_id="q_home")


GOOD = {
    "E2E-GUIDE-01": rec("act", "Done: I added the goal to book the tiler by Friday. Still open on this quest: Book the plumber, due 5 Nov.", [goal("Book the tiler")]),
    "E2E-GUIDE-02": rec("answer", "Your Riverside 10K is in {days} days, on 22 November, and you have 1,245 USD left under the launch cap."),
    "E2E-GUIDE-03": rec("act", "Done. I saved the grout note and added a goal to buy grout this weekend.",
                        [dict(tool="add_quest_note", args={"text": "grout must cure two days"}, quest_id="q_home"), goal("Buy grout")]),
    "E2E-GUIDE-04": rec("answer", "Start by booking the plumber: it is the one open goal, due 5 November, and the tiling waits on it. Want me to draft the message?"),
    "E2E-GUIDE-05": rec("answer", "Start with photographing the six candle scents: it is due 31 October and the launch page needs it. Want a shot list?"),
    "E2E-GUIDE-06": rec("answer", "Add a habit: give it a morning schedule and it shows on Today and Schedule. Log it each day. Want me to add it?"),
    "E2E-GUIDE-07": rec("answer", "Go to Settings, then Calendar Sync, connect Google Calendar and turn on showing events in Quest."),
    "E2E-GUIDE-08": rec("answer", "Yes, easy running is fine this week if there is no pain, as your physio said. Keep it easy ahead of the Riverside 10K."),
    "E2E-GUIDE-09": rec("answer", "Here it is: [launch summary](https://files.example.test/reports/launch-summary.md)."),
    "E2E-GUIDE-10": rec("answer", "I do not know a public link for reports/launch-summary.md, so here is what it says: soft launch on 3 Nov, 100 orders in 60 days, budget cap 2,000 USD."),
}
BAD = {
    "E2E-GUIDE-01": rec("clarify", "Could you do one thing at a time? Which should I do first?"),
    "E2E-GUIDE-02": rec("answer", "Which quest do you mean for the budget?"),
    "E2E-GUIDE-03": rec("act", "Added the goal. What would you like the note to say?", [goal("Buy grout")]),
    "E2E-GUIDE-04": rec("answer", "What would you like to focus on? Is it the plumbing, or the tiling? Tell me more?"),
    "E2E-GUIDE-05": rec("answer", "What do you want to do first? Tell me more about the launch?"),
    "E2E-GUIDE-06": rec("act", "I added a stretching habit.", [dict(tool="create_quest_habit", args={"name": "Stretch"}, quest_id="q_fit")]),
    "E2E-GUIDE-07": rec("answer", "I do not know how that works."),
    "E2E-GUIDE-08": rec("answer", "Easy running is fine. Your 10K is near. Stay focused on the Riverside goal, remember why you started, and never give up on the journey."),
    "E2E-GUIDE-09": rec("answer", "I cannot access files on the server."),
    "E2E-GUIDE-10": rec("answer", "See https://files.example.test/launch-summary for the file."),
}


def test_the_cases_are_whole_and_carry_their_source():
    assert len(CASES) == 14 and len({c["id"] for c in CASES}) == 14   # ten from the meeting, four held-out rewordings (GUIDE-11 to 14)
    assert all(len(c["source"]) > 60 and c["expect"] and c["quality_rubric"] for c in CASES)
    assert "—" not in json.dumps(CASES, ensure_ascii=False) and "–" not in json.dumps(CASES, ensure_ascii=False)


def test_a_right_turn_passes_every_case():
    days = score.countdown_problems  # noqa: F841  (the clock helper is exercised through reply_days_to below)
    from end_to_end.clock import world_today
    from datetime import date
    n = (date(2026, 11, 22) - world_today()).days
    for cid, got in GOOD.items():
        got = dict(got, reply=got["reply"].format(days=n) if "{days}" in got["reply"] else got["reply"])
        verdict = score.judge(got, BY_ID[cid])
        assert verdict["ok"], (cid, verdict["note"])


def test_a_wrong_turn_fails_every_case():
    for cid, got in BAD.items():
        assert not score.judge(got, BY_ID[cid])["ok"], cid


def test_the_context_lines_reach_the_planner_view():
    w = drive.new_world(BY_ID["E2E-GUIDE-09"])
    assert "https://files.example.test/reports/launch-summary.md" in w.context_view()
    assert "files.example" not in drive.new_world(BY_ID["E2E-GUIDE-01"]).context_view()
