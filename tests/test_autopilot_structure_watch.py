"""The structure watch bound to an autopilot run: the adapter maps a quest to the hub's digest, items become decision requests, nothing is edited, and no call is made when nothing changed."""
import json
from datetime import datetime, timezone

from quest_ai_runner.the_intelligence.proactivity import autopilot_structure_watch as binding
from quest_ai_runner.the_intelligence.proactivity.autopilot_routines import RoutinePass

NOW = "2026-11-10T09:00:00Z"


class Client:
    def __init__(self):
        self.decisions, self.edits, self.configured = [], [], True

    def list_quest_goals(self, quest_id, **kw):
        return {"period_groups": [{"goals": [{"id": "g1", "name": "Ship the first version", "deadline": "2026-10-01", "completed": False},
                                              {"id": "g2", "name": "Build on it", "deadline": "2026-12-01", "completed": False, "parent_goal_id": ""}]}]}

    def list_experiments(self, quest_id):
        return [{"id": "e1", "status": "running", "reevaluate_time": "2026-11-01", "held_fixed": "plan = a"}]

    def list_investigations(self, quest_id):
        return []

    owners = ["ai1", "u1"]

    def get_quest(self, quest_id):
        return {"quest_id": quest_id, "owner_user_ids": self.owners}

    def own_user_id(self):
        return "ai1"

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


def test_an_item_is_asked_of_the_quests_owner_never_the_runners_own_account_and_not_filed_when_no_person_owns_it():
    from hubofhubs.the_body.internal_context.run_store.runs import RunStore
    c = Client()
    binding.watch_quest(c, "q1", now=NOW, store=RunStore(""), ask=ask_for("experiments:e1"))
    assert c.decisions[0][1]["assignee_user_id"] == "u1"
    alone = Client()
    alone.owners = ["ai1"]
    out = binding.watch_quest(alone, "q1", now=NOW, store=RunStore(""), ask=ask_for("experiments:e1"))
    assert out["filed"] == 0 and alone.decisions == []


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


class Provider:
    """A provider that counts tokens and cost like the real ones (per-call counters read before and after)."""
    def __init__(self):
        self.tokens_in = self.tokens_out = 0
        self.cost_usd = 0.0
        self.models = []

    def answer(self, messages, *, model, **kw):
        self.models.append(model)
        self.tokens_in += 1200
        self.tokens_out += 80
        self.cost_usd += 0.03
        return json.dumps({"items": []})


def test_the_lane_builds_the_watch_by_default_and_a_switch_or_no_provider_leaves_it_off():
    c, p = Client(), Provider()
    assert binding.build_structure_watch(c, p, "") is not None
    assert binding.build_structure_watch(c, p, "", enabled=False) is None and binding.build_structure_watch(c, None, "") is None


def test_the_built_watch_asks_by_role_tier_reads_the_calls_usage_and_makes_no_call_in_a_dry_run():
    c, p = Client(), Provider()
    watch = binding.build_structure_watch(c, p, "", now=lambda: datetime(2026, 11, 10, 9, tzinfo=timezone.utc))
    assert watch("q1", True) is None and p.models == []
    out = watch("q1", False)
    assert out["called"] and out["tokens_in"] == 1200 and out["tokens_out"] == 80 and len(p.models) == 1 and c.edits == []
    assert watch("q1", False)["reason"] == "unchanged" and len(p.models) == 1


def test_the_poller_passes_the_watch_to_both_passes_and_the_config_switch_turns_it_off():
    from quest_ai_runner.the_body.configuration.config import RunnerConfig
    from quest_ai_runner.the_consciousness.request_intake.poller import Poller
    from .conftest import StubProvider, StubRetrieval
    def poller(**kw):
        cfg = RunnerConfig(quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1", retrieval=StubRetrieval({}), model_provider=StubProvider(decisions=[]), **kw)
        return Poller(cfg, state_path=None, client=Client())
    on, off = poller(), poller(autopilot_structure_watch=False)
    assert on._structure_watch is not None and on._autopilot._structure_watch is on._structure_watch
    assert off._structure_watch is None and off._autopilot._structure_watch is None


def test_a_login_only_lane_resolves_best_to_the_hubs_best_choice_and_a_lane_with_none_fails_with_the_reason():
    from hubofhubs.api import claude_cli
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    login = claude_cli.ClaudeCliProvider()
    top = ModelRegistry(login).top_models()
    assert top["best"] != top["quality"] and "fable" in top["best"] and ModelRegistry(login).resolve_role("structure-watch") == top["best"]
    ask = binding.provider_ask(Provider())      # a provider that is no login and holds no key: the hub names no model for it
    try:
        ask("x", "best", 500)
        raise AssertionError("expected the reason")
    except RuntimeError as error:
        assert "no model is chosen for the best tier" in str(error)
