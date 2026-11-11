"""The structure watch bound to an autopilot run: the adapter maps a quest to the hub's digest, items become decision requests, nothing is edited, and no call is made when nothing changed."""
import json
from datetime import datetime, timezone

from quest_ai_runner.the_intelligence.proactivity import autopilot_structure_watch as binding
from quest_ai_runner.the_intelligence.proactivity.autopilot_routines import RoutinePass

NOW = "2026-11-10T09:00:00Z"


class Client:
    def __init__(self):
        self.decisions, self.edits = [], []

    def list_quest_goals(self, quest_id, **kw):
        return {"period_groups": [{"goals": [{"id": "g1", "name": "Ship the first version", "deadline": "2026-10-01", "completed": False},
                                              {"id": "g2", "name": "Build on it", "deadline": "2026-12-01", "completed": False, "parent_goal_id": ""}]}]}

    def list_experiments(self, quest_id):
        return [{"id": "e1", "status": "running", "reevaluate_time": "2026-11-01", "held_fixed": "plan = a"}]

    def list_investigations(self, quest_id):
        return []

    def create_decision(self, summary, **kw):
        self.decisions.append((summary, kw))

    def update_goal(self, *a, **k):
        self.edits.append(a)


def ask_for(subject):
    calls = []

    def ask(prompt, tier, cap):
        calls.append((len(prompt), tier, cap))
        return json.dumps({"items": [{"subject": subject, "what": "An experiment is past its reevaluation.", "why_now": "work continues on it", "risk": "choices stay fixed too long",
                                      "severity": "high", "step": "reevaluate", "step_text": "Reevaluate it now."}]})
    ask.calls = calls
    return ask


def test_a_quests_records_map_to_the_hubs_digest_shape():
    c = Client()
    s = binding.structure_of("q1", "outcome", binding.goals_of(c.list_quest_goals("q1")), c.list_experiments("q1"), [], [], NOW)
    assert [m["id"] for m in s["tables"]["milestones"]] == ["g1", "g2"] and s["tables"]["experiments"][0]["reevaluate_time"] == "2026-11-01" and "review" not in s


def test_an_item_is_a_decision_request_and_no_record_is_edited():
    from hubofhubs.the_body.internal_context.run_store.runs import RunStore
    c, ask = Client(), ask_for("experiments:e1")
    out = binding.watch_quest(c, "q1", now=NOW, store=RunStore(""), ask=ask)
    assert out["called"] and out["filed"] == 1 and len(c.decisions) == 1 and c.edits == []
    assert c.decisions[0][1]["default_on_silence"] == "hold" and c.decisions[0][1]["quest_id"] == "q1" and ask.calls[0][1] in ("best", "strong")


def test_no_call_when_nothing_changed_since_the_last_run_and_none_in_a_dry_run():
    from hubofhubs.the_body.internal_context.run_store.runs import RunStore
    c, ask, store = Client(), ask_for("experiments:e1"), RunStore("")
    binding.watch_quest(c, "q1", now=NOW, store=store, ask=ask)
    again = binding.watch_quest(c, "q1", now="2026-11-10T10:00:00Z", store=store, ask=ask)
    assert again["called"] is False and again["reason"] == "unchanged" and len(ask.calls) == 1
    assert binding.watch_quest(c, "q1", now=NOW, store=store, ask=ask, dry_run=True)["reason"] == "off" and len(ask.calls) == 1


def test_a_routine_run_calls_the_watch_once_and_a_run_at_its_ceiling_makes_no_call():
    from hubofhubs.the_body.internal_context.run_store.runs import RunStore
    c, ask = Client(), ask_for("experiments:e1")
    out = binding.watch_quest(c, "q1", now=NOW, store=RunStore(""), ask=ask, ceilings={"spent_usd": 9.0, "ceiling_usd": 10.0})
    assert out["called"] is False and out["reason"] == "ceiling" and ask.calls == []
    seen = []
    routine = RoutinePass(c, structure_watch=lambda quest_id, dry: seen.append((quest_id, dry)))
    assert routine._structure_watch is not None and seen == []
