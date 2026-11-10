"""Experiments as a core input of the runner: the folder block, the prompt section, the default
brief, and the one-open-proposal rule.

The experiment rows below have the shape of Quest's real ``GET /api/quests/{id}/research-questions``
response (quest-backend ``app/models/research_question.py``); the untouched default row is a
verbatim copy of what the personal account's quests return today.
"""
import pytest

from quest_ai_runner.the_intelligence.proactivity.autopilot import AutopilotPass, compose_batch_text
from quest_ai_runner.the_intelligence.proactivity.autopilot_defaults import BUNDLED_DEFAULT_QUEST_INSTRUCTIONS
from quest_ai_runner.the_intelligence.proactivity.autopilot_personas import default_quest_instructions_for
from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestClient
from quest_ai_runner.the_intelligence.higher_mind.objectives.quest_experiments import (
    EXPERIMENT_PROPOSAL_KIND, EXPERIMENTS_END, EXPERIMENTS_START, experiments_brief_section, fetch_experiments, has_experiment, proposal_is_open,
    render_experiments_block, render_experiments_brief)
from quest_ai_runner.the_intelligence.higher_mind.objectives.quest_folder_sync import pull_quest_to_folder, render_sync_file
from quest_ai_runner.the_intelligence.higher_mind.objectives.quest_folder_zones import ensure_folder_zones

from .test_autopilot import FakeAutopilotClient, _goal, _goals_payload, _now, _quest
from .test_quest_folder_sync import QUEST_ID, MockQuestFolderClient

DEFAULT_ROW = {
    "research_question_id": "rq_default0001", "quest_id": QUEST_ID, "user_id": "user_1",
    "kind": "default", "question": "What are the most effective strategies for someone to achieve "
    "this outcome, given their current state, preferences and timeline?",
    "context": "Outcome: Follow a routine I feel fully aligned with\nCurrent state: ",
    "hypotheses": [], "purpose": "", "sub_questions": [], "assumptions": [], "status": "open",
    "deep_inquiry_ids": [], "linked_goal_ids": [], "linked_collection_ids": [],
    "start_date": None, "end_date": None, "conditions": [], "analyses": [], "conclusion": "",
    "concluded_at": None, "created_at": "2026-10-14T13:52:51.256132",
    "updated_at": "2026-10-14T13:52:51.256132",
}


def running_row(**over):
    row = dict(DEFAULT_ROW, research_question_id="rq_oats", kind="custom",
               question="Does oat breakfast beat eggs before long runs?",
               context="Long runs are on Sundays.\n## not a heading",
               purpose="Run the marathon faster", status="experimenting",
               sub_questions=[{"sub_question_id": "sq1", "question": "Which feels lighter?",
                               "answer_method": "read my notes"}],
               hypotheses=[{"hypothesis_id": "h1", "statement": "Oats give a faster pace",
                            "sub_question_id": None},
                           {"hypothesis_id": "h2", "statement": "Eggs feel heavier",
                            "sub_question_id": "sq1"}],
               assumptions=["Sleep is constant"],
               linked_goal_ids=["goal_a"], linked_collection_ids=["coll_run"],
               start_date="2026-10-01", end_date="2026-11-01",
               conditions=[{"condition_id": "c1", "name": "Long run", "definition": "Run 10 km+",
                            "required": True, "metric": "days_done", "period": "week",
                            "minimum": 1, "daily_minimum": None, "days_of_week": [6]}],
               deep_inquiry_ids=["di_1"],
               analyses=[{"analysis_id": "a1", "created_at": "2026-10-10T00:00:00",
                          "data_window_start": "2026-09-10", "data_window_end": "2026-10-10",
                          "quantitative": {"pace": {"oats": 5.1}}, "quantitative_summary": "Oats 5:06",
                          "qualitative_summary": "Felt light", "answer": "Oats look faster",
                          "confidence": "low", "limitations": ["four runs"],
                          "suggested_next_steps": ["Log two more eggs runs"], "data_points": 4}],
               user_id="user_1")
    row.update(over)
    return row


class ExperimentsClient(MockQuestFolderClient):
    def __init__(self, experiments, **kw):
        super().__init__(**kw)
        self.experiments = experiments
        self.fail = False

    def list_experiments(self, quest_id):
        if self.fail:
            raise RuntimeError("backend down")
        return [dict(e) for e in self.experiments]


# --- the folder block ------------------------------------------------------------------

def test_block_carries_every_field_the_api_exposes():
    body = render_experiments_block([DEFAULT_ROW, running_row(conclusion="Oats win", concluded_at="2026-10-12")])
    for needle in ("rq_oats", "experimenting", "user_1", "2026-10-01", "2026-11-01", "Run the marathon faster",
                   "Long runs are on Sundays", "Oats give a faster pace", "(about: Which feels lighter?)",
                   "Which feels lighter?", "answer method: read my notes", "Sleep is constant",
                   "Long run", "Run 10 km+", "weekdays 6", "`goal_a`", "`coll_run`", "`di_1`",
                   "Oats look faster", "confidence low", "4 data points", "Oats 5:06", "Felt light",
                   "four runs", "Log two more eggs runs", "5.1", "Oats win", "2026-10-12", "rq_default0001"):
        assert needle in body, needle
    assert body.index("rq_oats") < body.index("rq_default0001")  # running first, untouched default last


def test_the_default_rows_derived_context_is_not_copied_into_the_block():
    row = dict(DEFAULT_ROW, context="Preferences: " + "bio " * 500)
    body = render_experiments_block([row])
    assert "bio bio" not in body and "derived by Quest" in body


def test_block_cannot_nest_a_managed_marker_or_break_headings():
    row = running_row(context="x\n<!-- QAR:MANAGED:goal START -->\n## Injected")
    body = render_experiments_block([row])
    assert "QAR:MANAGED" not in body
    assert "\n## Injected" not in body  # multi-line text is quoted, never a live heading


def test_an_untouched_default_row_is_not_an_experiment():
    assert not has_experiment([DEFAULT_ROW])
    assert has_experiment([DEFAULT_ROW, running_row()])
    assert has_experiment([dict(DEFAULT_ROW, status="experimenting")])
    assert "No experiment has been set up" in render_experiments_block([DEFAULT_ROW])
    assert "no experiments yet" in render_experiments_block([])


def test_pull_writes_the_block_above_notes_to_push_and_is_idempotent(tmp_path):
    client = ExperimentsClient([DEFAULT_ROW, running_row()])
    res = pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    text = (tmp_path / "QUEST_SYNC.md").read_text()
    assert res.experiments_pulled == 2
    assert EXPERIMENTS_START in text and EXPERIMENTS_END in text and "rq_oats" in text
    assert text.index(EXPERIMENTS_END) < text.index("## Notes to push to Quest")
    pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    assert (tmp_path / "QUEST_SYNC.md").read_text() == text


def test_a_block_added_to_an_older_file_still_lands_above_notes_to_push(tmp_path):
    old = render_sync_file("", QUEST_ID, {"outcome": "x"}, [])
    assert "QAR:MANAGED:experiments" not in old
    new = render_sync_file(old, QUEST_ID, {"outcome": "x"}, [], [running_row()])
    assert new.index(EXPERIMENTS_END) < new.index("## Notes to push to Quest")


def test_hand_edits_inside_the_block_are_overwritten_and_never_pushed(tmp_path):
    client = ExperimentsClient([running_row()])
    pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    path = tmp_path / "QUEST_SYNC.md"
    path.write_text(path.read_text().replace("Oats look faster", "HAND EDIT"))
    pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    assert "HAND EDIT" not in path.read_text() and "Oats look faster" in path.read_text()
    assert not hasattr(client, "update_experiment")  # the sync has no write path to experiments


def test_a_failed_experiment_read_keeps_the_last_good_block(tmp_path):
    client = ExperimentsClient([running_row()])
    pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    before = (tmp_path / "QUEST_SYNC.md").read_text()
    client.fail = True
    res = pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    assert res.experiments_pulled is None
    assert (tmp_path / "QUEST_SYNC.md").read_text() == before


def test_a_client_without_experiment_support_still_pulls(tmp_path):
    res = pull_quest_to_folder(MockQuestFolderClient(), QUEST_ID, str(tmp_path))
    assert res.pulled and res.experiments_pulled is None
    assert "QAR:MANAGED:experiments" not in (tmp_path / "QUEST_SYNC.md").read_text()


def test_the_folder_map_text_mentions_experiments(tmp_path):
    ensure_folder_zones(str(tmp_path), quest_id=QUEST_ID)
    assert "experiments" in (tmp_path / "CLAUDE.md").read_text()


# --- the compact prompt form -----------------------------------------------------------

def test_brief_marks_running_experiments_as_things_to_carry_out():
    out = render_experiments_brief([DEFAULT_ROW, running_row()])
    assert "[RUNNING, help carry it out] Does oat breakfast" in out
    assert "Log two more eggs runs" in out and "QUEST_SYNC.md" in out


def test_brief_stays_inside_its_budget_and_keeps_running_ones_first():
    many = [running_row(research_question_id=f"rq_{i}", question=f"Question number {i} " + "w " * 60,
                        status="concluded" if i else "experimenting") for i in range(40)]
    out = render_experiments_brief(many, max_chars=3000)
    assert len(out) <= 3000
    assert "rq_0" in out and "[RUNNING" in out
    assert "more, in QUEST_SYNC.md" in out


def test_section_carries_running_experiments_forward_whatever_the_planning_setting():
    for planning in ("work_only", "plan_and_work"):
        out = experiments_brief_section([running_row()], quest_id="q1", planning=planning)
        assert "RUNNING" in out and "Do not start, conclude, rewrite or spend money" in out


def test_section_proposes_when_none_exist_and_the_quest_may_plan():
    out = experiments_brief_section([DEFAULT_ROW], quest_id="q1", planning="plan_and_work")
    assert EXPERIMENT_PROPOSAL_KIND in out and "create_decision" in out
    assert "hypothesis" in out and "minimal design" in out and "Never create or start" in out


def test_section_is_silent_when_proposing_would_be_planning_the_quest_withheld():
    assert experiments_brief_section([DEFAULT_ROW], quest_id="q1", planning="work_only") == ""


def test_section_never_claims_none_when_the_read_failed():
    assert experiments_brief_section(None, quest_id="q1", planning="plan_and_work") == ""


def test_section_stops_asking_when_a_proposal_is_open_or_another_batch_is_proposing():
    for kw in ({"proposal_open": True}, {"may_propose": False}):
        out = experiments_brief_section([DEFAULT_ROW], quest_id="q1", planning="plan_and_work", **kw)
        assert "Do not raise another" in out and "create_decision" not in out


# --- composition and the pass ----------------------------------------------------------

def test_compose_includes_experiments_and_is_byte_identical_without_them():
    section = experiments_brief_section([running_row()], quest_id="q1")
    with_exp = compose_batch_text("Ship it", scope_label="day:2026-07-12", experiments=section)
    assert section in with_exp
    base = compose_batch_text("Ship it", scope_label="day:2026-07-12")
    assert compose_batch_text("Ship it", scope_label="day:2026-07-12", experiments=None) == base
    assert "Experiments on this quest" not in base


class PassClient(FakeAutopilotClient):
    def __init__(self, experiments, decisions=(), **kw):
        super().__init__(**kw)
        self.experiments = experiments
        self.decisions = list(decisions)

    def list_experiments(self, quest_id):
        return list(self.experiments)

    def list_open_decisions_for_quest(self, quest_id):
        return list(self.decisions)


def run_pass(experiments, planning, decisions=()):
    q = _quest("q1", mode="act", planning=planning)
    client = PassClient(experiments, decisions, quests=[q], goals_by_quest={})
    AutopilotPass(client, team_id="team1", daily_budget=3, now=_now).run({"text": "pass"})
    return client.created_tasks[0]["text"]


def test_the_pass_hands_a_running_experiment_to_the_batch():
    text = run_pass([DEFAULT_ROW, running_row()], "work_only")
    assert "Does oat breakfast beat eggs" in text and "RUNNING" in text


def test_the_pass_proposes_on_a_quest_with_none_that_may_plan():
    assert "create_decision" in run_pass([DEFAULT_ROW], "plan_and_work")


def test_the_pass_does_not_propose_while_one_is_open():
    text = run_pass([DEFAULT_ROW], "plan_and_work",
                    decisions=[{"id": "d1", "status": "open", "kind": EXPERIMENT_PROPOSAL_KIND}])
    assert "Do not raise another" in text and "Propose ONE experiment" not in text


def test_the_pass_does_not_propose_on_a_work_only_quest():
    text = run_pass([DEFAULT_ROW], "work_only")
    assert "Propose ONE experiment" not in text and "Experiments on this quest" not in text


def test_an_unreadable_experiment_list_adds_nothing_to_the_batch():
    class Broken(PassClient):
        def list_experiments(self, quest_id):
            raise RuntimeError("down")
    q = _quest("q1", mode="act", planning="plan_and_work")
    client = Broken([], quests=[q], goals_by_quest={})
    AutopilotPass(client, team_id="team1", daily_budget=3, now=_now).run({"text": "pass"})
    text = client.created_tasks[0]["text"]
    assert "Propose ONE experiment" not in text and "Experiments on this quest" not in text
    assert "no experiment of its own" not in text


def test_fetch_and_proposal_helpers_distinguish_failure_from_empty():
    assert fetch_experiments(object(), "q") is None
    assert fetch_experiments(ExperimentsClient([]), "q") == []
    bad = ExperimentsClient([]); bad.fail = True
    assert fetch_experiments(bad, "q") is None

    class Lister:
        def __init__(self, rows=None, boom=False):
            self.rows, self.boom = rows or [], boom

        def list_open_decisions_for_quest(self, q):
            if self.boom:
                raise RuntimeError("x")
            return self.rows
    assert proposal_is_open(Lister([{"kind": EXPERIMENT_PROPOSAL_KIND}]), "q") is True
    assert proposal_is_open(Lister([{"kind": "approve"}]), "q") is False
    assert proposal_is_open(Lister(boom=True), "q") is True  # fail closed: no duplicate on doubt
    assert proposal_is_open(object(), "q") is False


# --- the default brief: one source, experiments in it ----------------------------------

def test_the_default_quest_brief_names_experiments_as_a_core_concern():
    text = BUNDLED_DEFAULT_QUEST_INSTRUCTIONS
    assert text.count("Carry each running experiment forward") == 1
    for needle in ("Never start, change, conclude or spend money",
                   "hypothesis", "metric", "minimal design", "cost", "single ask",
                   "your brief says you may plan"):
        assert needle in text, needle
    assert "—" not in text  # no em dashes


def test_a_served_default_still_wins_over_the_bundled_one():
    """Single source: the backend's text is what runs; the bundle is only the older-backend floor."""
    assert default_quest_instructions_for({"default_quest_instructions": "SERVED"}) == "SERVED"
    assert default_quest_instructions_for({}) == BUNDLED_DEFAULT_QUEST_INSTRUCTIONS


def test_the_default_brief_reaches_a_defaulted_batch_with_the_experiments_section():
    q = _quest("q1", mode="act", planning="plan_and_work")
    client = PassClient([DEFAULT_ROW], quests=[q], goals_by_quest={})
    AutopilotPass(client, team_id="team1", daily_budget=3, now=_now).run({"text": "pass"})
    text = client.created_tasks[0]["text"]
    assert "Carry each running experiment forward" in text   # the standing brief
    assert "Propose ONE experiment" in text   # the brief's own experiments section


# --- at most one open proposal, enforced where it is created --------------------------------

def make_client(open_rows):
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    sent = []

    def fake_request(method, path, *, params=None, body=None):
        if method == "GET":
            return list(open_rows)
        sent.append(body)
        return {"decision_id": "dec_new", "kind": body["kind"]}

    client._request = fake_request  # type: ignore[assignment]
    return client, sent


def test_a_second_experiment_proposal_is_not_created_while_one_is_open():
    existing = {"decision_id": "dec_old", "status": "open", "kind": EXPERIMENT_PROPOSAL_KIND}
    client, sent = make_client([existing])
    out = client.create_decision("Try oats vs eggs", kind=EXPERIMENT_PROPOSAL_KIND, quest_id="q1")
    assert out["decision_id"] == "dec_old" and sent == []


def test_an_experiment_proposal_is_created_when_none_is_open_and_other_kinds_are_untouched():
    client, sent = make_client([{"decision_id": "d9", "status": "open", "kind": "approve"}])
    assert client.create_decision("Try oats", kind=EXPERIMENT_PROPOSAL_KIND,
                                  quest_id="q1")["decision_id"] == "dec_new"
    client.create_decision("Pay invoice", kind="approve", quest_id="q1")
    assert [b["kind"] for b in sent] == [EXPERIMENT_PROPOSAL_KIND, "approve"]


def focus_row(**over):
    base = dict(research_question_id="rq_focus", question="Is writing first helping?", research_question="Write before any email",
                phase="running", reevaluation_due=False, days_to_reevaluation=9, reevaluate_time="2026-11-19",
                owner_user_ids=["user_2"], held_fixed=["Write 6 to 8 am", "Same chapter order"], in_scope=["Morning writing"],
                outside_scope=["Changing the outline"], reevaluation_notes=[{"note_id": "n1", "text": "Try evenings", "at": "2026-11-10"}],
                reevaluations=[])
    return running_row(**{**base, **over})


def test_trial_period_shows_focus_attention_held_fixed_and_the_date_in_the_prompt_and_the_folder():
    rows = [DEFAULT_ROW, focus_row()]
    brief = experiments_brief_section(rows, quest_id=QUEST_ID)
    assert "Write before any email" in brief and "reevaluate on 2026-11-19" in brief
    assert "held fixed until then (do not reopen): Write 6 to 8 am; Same chapter order" in brief and "out of scope: Changing the outline" in brief
    assert "thoughts set aside for the reevaluation: Try evenings" in brief and "owner: user_2" in brief
    assert "NOT re-decided until its reevaluation date" in brief and "REEVALUATION DUE:" not in brief
    block = render_experiments_block(rows)
    assert "**Held fixed until then (do not reopen):** Write 6 to 8 am; Same chapter order" in block and "**Status:** running (a trial period)" in block


def test_a_due_reevaluation_comes_first_and_is_asked_for_not_recorded():
    due = focus_row(research_question_id="rq_due", research_question="Cold showers")
    due.update(reevaluation_due=True, phase="reevaluating", reevaluate_time="2026-11-09")
    two = [focus_row(), due]
    text = experiments_brief_section(two, quest_id=QUEST_ID)
    assert "REEVALUATION DUE: 1 experiment(s)" in text and "experiment_reevaluation" in text and "Do not record the reevaluation yourself" in text
    assert text.index("Cold showers") < text.index("Write before any email")       # due ones lead, and two experiments run at once
    assert "[REEVALUATION DUE, ask what was learned]" in text


def test_an_older_row_with_no_focus_fields_reads_as_before():
    old = running_row()
    assert "research question:" not in render_experiments_brief([old]) and "[RUNNING, help carry it out]" in render_experiments_brief([old])


def test_update_tool_refuses_to_reopen_what_a_running_experiment_holds_fixed_and_sets_the_thought_aside():
    from quest_ai_runner.the_body.hands.quest_operations.tools_quest_experiments import update_quest_experiment_spec
    from quest_ai_runner.the_world.tools.tools import ToolContext

    calls = []

    class FakeClient:
        def list_experiments(self, quest_id):
            return [focus_row()]

        def update_experiment(self, quest_id, rid, **changes):
            calls.append(("update", changes))
            return {"research_question_id": rid, "status": "experimenting", "phase": "running"}

        def add_experiment_note(self, quest_id, rid, text):
            calls.append(("note", text))
            return {"research_question_id": rid, "status": "experimenting", "phase": "running"}

    spec = update_quest_experiment_spec({}, client_factory=lambda: FakeClient())
    ctx = ToolContext(quest_id=QUEST_ID) if "quest_id" in getattr(ToolContext, "__dataclass_fields__", {}) else ToolContext()
    base = {"quest_id": QUEST_ID, "research_question_id": "rq_focus", "user_asked_for_this_field": True}
    refused = spec.handler({**base, "held_fixed": ["Write at night"]}, ctx)
    assert not refused.ok and "note_for_reevaluation" in refused.text and not calls
    noted = spec.handler({**base, "note_for_reevaluation": "Maybe write at night"}, ctx)
    assert noted.ok and calls == [("note", "Maybe write at night")]
    chosen = spec.handler({**base, "held_fixed": ["Write at night"], "person_chose_to_reopen": True}, ctx)
    assert chosen.ok and calls[-1] == ("update", {"held_fixed": ["Write at night"]})
