"""Incremental replies and asks: the pure interim policy, the ask policy, and the goal loop wired to both. Fully
offline: scripted providers, a stub escalation sink, no network, no model.

Run: .venv/bin/python -m pytest tests/test_incremental_replies.py -q -p no:cacheprovider
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List

from hubofhubs.act.tool_ledger import ToolOutcome
from hubofhubs.ports.deep import DeepResult
from hubofhubs.ports.events import EVENT_DECISION, EVENT_INTERIM, EVENT_RESULT
from quest_ai_runner.core.adapters_sinks import StreamSink
from quest_ai_runner.core.ask_policy import (
    Assignee, ask_dedupe_key, blocks_on_person, file_ask, resolve_assignee, stop_reason_of)
from quest_ai_runner.core.interim_policy import (
    InterimLedger, InterimState, decide, done_text, interim_event_data, interim_text_of, progress_signature)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubRetrieval
from .test_done_evidence import ANALYSIS, LOOKUP, Provider, Runner


def state(**over: Any) -> InterimState:
    base = InterimState(enabled=True, met=False, blocker="more_work", loop_runs_again=True, ask_open=False, has_why=True,
                        has_content=True, sent_count=0, last_sent_at=None, now=100.0,
                        signature=progress_signature(output="a", receipts=["r1"], coverage=None), last_signature=None)
    return replace(base, **over)


# ---- the pure policy

def test_the_policy_sends_when_every_condition_holds():
    assert decide(state()).send


def test_each_failing_condition_skips_with_its_own_reason():
    cases = {
        "disabled": dict(enabled=False), "blocker_is_not_more_work": dict(blocker="needs_person"),
        "loop_does_not_run_again": dict(loop_runs_again=False), "an_ask_is_open": dict(ask_open=True),
        "no_reason_to_give": dict(has_why=False), "nothing_to_show": dict(has_content=False),
        "per_turn_cap_reached": dict(sent_count=4), "too_soon_after_last_message": dict(last_sent_at=95.0)}
    for reason, over in cases.items():
        d = decide(state(**over))
        assert (d.send, d.reason) == (False, reason)
    assert decide(state(met=True)).reason == "blocker_is_not_more_work"


def test_no_delta_no_message_and_a_change_in_any_part_is_a_delta():
    first = progress_signature(output="a", receipts=["r1"], coverage=None)
    assert decide(state(signature=first, last_signature=first)).reason == "nothing_new_since_last_message"
    assert progress_signature(output=" a ", receipts=["r1"], coverage=None) == first   # reflow is not news
    # a reworded account of the same work is not news once a message has been sent
    assert decide(state(signature=progress_signature(output="b", receipts=["r1"], coverage=None), last_signature=first)).reason \
        == "nothing_new_since_last_message"
    for changed in (progress_signature(output="a", receipts=["r1", "r2"], coverage=None),
                    progress_signature(output="a", receipts=["r1"], coverage={"processed": 5, "total": 9})):
        assert decide(state(signature=changed, last_signature=first)).send


def test_spacing_is_measured_from_the_last_message_and_the_cap_is_per_turn():
    assert decide(state(last_sent_at=84.0)).send
    assert not decide(state(last_sent_at=86.0)).send
    ledger = InterimLedger()
    assert [ledger.record(float(i), ()) for i in range(3)] == [1, 2, 3]
    assert ledger.sent_count == 3 and ledger.last_sent_at == 2.0


def test_done_comes_from_receipts_and_coverage_and_never_from_the_worker_account():
    done = done_text(receipts=["Added the goal X"], coverage={"processed": 2, "total": 5, "unit": "entries"})
    assert "- Added the goal X" in done and "2 of 5" in done
    assert done_text(receipts=[], coverage=None) == ""   # a worker that reports nothing gives nothing to show


def test_the_interim_text_has_done_why_and_next_and_no_em_dash():
    data = interim_event_data(done="- Added X", why_more="two entries remain", next_step="read them", coverage=None, attempt=1, seq=1)
    assert data["will_continue"] is True and data["final"] is False and data["seq"] == 1
    text = interim_text_of(data)
    assert "two entries remain" in text and "read them" in text and "—" not in text


# ---- the ask policy

def test_only_a_person_block_is_an_ask_and_a_platform_fault_is_not():
    fixable = ToolOutcome(tool="t", ok=False, signature="t|403|forbidden")
    fault = ToolOutcome(tool="t", ok=False, signature="t|500|server error")
    assert blocks_on_person("needs_person", "Which account?")
    assert not blocks_on_person("needs_person", "")
    assert blocks_on_person("tool_failure", "no access", [fixable])
    assert not blocks_on_person("tool_failure", "down", [fault])
    assert not blocks_on_person("tool_failure", "down", [])
    assert not blocks_on_person("more_work", "q")
    assert stop_reason_of(exhausted_budget=True, stalled=False, tool_failure=True, attempts_spent=True) == "tool_fault"
    assert stop_reason_of(exhausted_budget=True, stalled=False, tool_failure=False, attempts_spent=True) == "budget"
    assert stop_reason_of(exhausted_budget=False, stalled=True, tool_failure=False, attempts_spent=True) == "stalled"
    assert stop_reason_of(exhausted_budget=False, stalled=False, tool_failure=False, attempts_spent=True) == "attempt_cap"


def test_the_assignee_is_the_asker_unless_a_participant_resolves():
    people = [{"user_id": "u1", "name": "Ana Lopez"}, {"user_id": "u2", "name": "Ben Ito"}]
    assert resolve_assignee("asker", "", people, "u1") == Assignee("u1", "asker")
    assert resolve_assignee("member", "u2", people, "u1") == Assignee("u2", "member")
    assert resolve_assignee("member", "Ben", people, "u1") == Assignee("u2", "member")
    assert resolve_assignee("member", "nobody", people, "u1") == Assignee("u1", "asker", fell_back=True)
    assert resolve_assignee("member", "u1", people, "u1") == Assignee("u1", "asker")


def test_the_dedupe_key_is_stable_per_turn_and_gap():
    assert ask_dedupe_key("t1", "c", "Which  account?") == ask_dedupe_key("t1", "c", "which account?")
    assert ask_dedupe_key("t1", "c", "a") != ask_dedupe_key("t2", "c", "a")


class Sink:
    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    def escalate(self, escalation):
        if self.fail:
            raise RuntimeError("down")
        self.sent.append(escalation)
        return "d-1"


def test_file_ask_returns_an_id_or_an_honest_failure():
    ok = file_ask(Sink(), question="q", why_only_you="w", done="", options=None, quest_id="Q", conv_id="c", turn_id="t",
                  asker_user_id="u1", assignee=Assignee("u1", "asker"), summary="q")
    assert ok.filed and ok.decision_id == "d-1"
    bad = file_ask(Sink(fail=True), question="q", why_only_you="", done="", options=None, quest_id=None, conv_id="", turn_id="",
                   asker_user_id="", assignee=Assignee("", "asker"), summary="q")
    assert not bad.filed and "down" in bad.error
    assert not file_ask(None, question="q", why_only_you="", done="", options=None, quest_id=None, conv_id="", turn_id="",
                        asker_user_id="", assignee=Assignee("", "asker"), summary="q").filed


# ---- the goal loop

def build(provider, runner, sink=None, **cfg):
    cfg.setdefault("deep_goal_max_iterations", 4)
    return Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), deep_runner=runner,
                        escalation=sink, config=OrchestratorConfig(**cfg))


def run(orch, meta=None) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    orch.run("do the thing", sink=StreamSink(events.append), context_meta=meta) if meta else orch.run("do the thing", sink=StreamSink(events.append))
    return events


NOT_MET = {"met": False, "reason": "two entries are still missing", "next_action": "read the last two", "blocker": "more_work",
           "progress_gap": "two entries are still missing", "progress_next": "read the last two"}


def receipts(text, *lines):
    return DeepResult(met=True, output=text, observations=list(lines), observations_reported=True, has_write_receipt=True)


def test_incremental_replies_off_sends_no_new_style_interim():
    provider = Provider([dict(ANALYSIS, work_shape="action")], verdicts=[dict(NOT_MET), {"met": True}])
    events = run(build(provider, Runner([receipts("first", "Added A"), receipts("all", "Added A", "Added B")])))
    assert not [e for e in events if e["type"] == EVENT_INTERIM]


def test_an_unmet_turn_that_will_run_again_sends_one_interim_with_why_and_next_built_from_receipts():
    provider = Provider([dict(ANALYSIS, work_shape="action")], verdicts=[dict(NOT_MET), {"met": True}])
    orch = build(provider, Runner([receipts("claimed it all", "Added A"), receipts("all", "Added A", "Added B")]),
                 interim_replies=True, min_seconds_between_interims=0.0)
    events = run(orch)
    interim = [e for e in events if e["type"] == EVENT_INTERIM]
    assert len(interim) == 1
    data = interim[0]["data"]
    assert data["why_more"] == "two entries are still missing" and data["next"] == "read the last two"
    assert "- Added A" in data["done"] and data["will_continue"] is True and data["seq"] == 1
    result = [e for e in events if e["type"] == EVENT_RESULT][-1]
    assert result["data"]["outcome"] == "met"


def test_the_last_attempt_never_produces_an_interim_it_produces_a_stopped_final():
    provider = Provider([dict(ANALYSIS, work_shape="action")], verdicts=[dict(NOT_MET), dict(NOT_MET)])
    orch = build(provider, Runner([receipts("a", "Added A"), receipts("b", "Added A", "Added B")]),
                 interim_replies=True, min_seconds_between_interims=0.0, deep_goal_max_iterations=2)
    events = run(orch)
    assert len([e for e in events if e["type"] == EVENT_INTERIM]) == 1   # after attempt 1 only
    result = [e for e in events if e["type"] == EVENT_RESULT][-1]
    assert result["data"]["outcome"] == "stopped" and result["data"]["stop_reason"] == "attempt_cap"


def test_a_needs_person_verdict_files_one_ask_and_emits_it_not_an_interim():
    verdict = {"met": False, "reason": "cannot choose the account", "blocker": "needs_person",
               "question": "Which account should I use?"}
    provider = Provider([dict(ANALYSIS, work_shape="action")], verdicts=[verdict])
    sink = Sink()
    orch = build(provider, Runner([receipts("partial", "Read the files")]), sink=sink, interim_replies=True)
    events = run(orch, meta={"asker_user_id": "u1", "conv_id": "c1", "turn_id": "t1", "quest_id": "Q1",
                             "participants": [{"user_id": "u1", "name": "Ana"}]})
    asks = [e for e in events if e["type"] == EVENT_DECISION]
    assert len(sink.sent) == 1 and sink.sent[0].dedupe_key and sink.sent[0].notify_user_id == "u1"
    assert sink.sent[0].assignee == "u1" and sink.sent[0].conv_id == "c1"
    assert len(asks) == 1 and asks[0]["decision_id"] == "d-1"
    assert asks[0]["data"]["outcome"] == "asked" and asks[0]["data"]["question"] == "Which account should I use?"
    assert "Which account should I use?" in asks[0]["text"] and "—" not in asks[0]["text"]
    assert not [e for e in events if e["type"] == EVENT_INTERIM]


def test_a_platform_fault_verdict_is_not_an_ask():
    verdict = {"met": False, "reason": "the service is down", "blocker": "tool_failure", "question": "The tool answered 500."}
    sink = Sink()
    orch = build(Provider([dict(ANALYSIS, work_shape="action")], verdicts=[verdict, verdict]),
                 Runner([receipts("x", "Read"), receipts("y", "Read")]), sink=sink)
    events = run(orch)
    assert not sink.sent and not [e for e in events if e["type"] == EVENT_DECISION]
    result = [e for e in events if e["type"] == EVENT_RESULT][-1]
    assert result["data"]["outcome"] == "stopped"


def test_an_ask_to_a_teammate_names_them_and_the_ask_is_filed_to_them():
    verdict = {"met": False, "reason": "only the treasurer can release it", "blocker": "needs_person", "asked_of": "member",
               "member_ref": "u2", "question": "May the deposit be released?"}
    provider = Provider([dict(ANALYSIS, work_shape="action")], verdicts=[verdict])
    sink = Sink()
    orch = build(provider, Runner([receipts("partial", "Read the files")]), sink=sink, interim_replies=True)
    events = run(orch, meta={"asker_user_id": "u1", "conv_id": "c1", "turn_id": "t1",
                             "participants": [{"user_id": "u1", "name": "Ana"}, {"user_id": "u2", "name": "Bram"}]})
    ask = [e for e in events if e["type"] == EVENT_DECISION][0]
    assert sink.sent[0].assignee == "u2" and sink.sent[0].notify_user_id == "u1"
    assert ask["text"].endswith("To finish this I need one thing from Bram: May the deposit be released?")
    assert "—" not in ask["text"]


def test_the_verifier_is_given_the_participants_of_the_turn():
    seen = {}
    provider = Provider([dict(ANALYSIS, work_shape="action")], verdicts=[{"met": True}])
    orch = build(provider, Runner([receipts("all", "Added A")]))
    original = orch._verify_goal

    def spy(*a, **k):
        seen["participants"] = k.get("participants")
        return original(*a, **k)
    orch._verify_goal = spy
    run(orch, meta={"participants": [{"user_id": "u1", "name": "Ana"}, {"user_id": "u2", "name": "Bram"}]})
    assert [p["user_id"] for p in seen["participants"]] == ["u1", "u2"]


def test_an_answer_turn_blocked_on_a_person_files_one_ask_when_switched_on_and_none_when_off():
    verdict = {"met": False, "reason": "needs the trip dates", "blocker": "needs_person", "question": "What are the trip dates?"}
    meta = {"asker_user_id": "u1", "conv_id": "c1", "turn_id": "t1", "participants": [{"user_id": "u1", "name": "Ana"}]}
    for switched_on in (True, False):
        sink = Sink()
        orch = build(Provider([dict(LOOKUP)], verdicts=[dict(verdict), dict(verdict)]), Runner([]), sink=sink,
                     interim_replies=switched_on, answer_goal_max_iterations=3)
        events = run(orch, meta=meta)
        asks = [e for e in events if e["type"] == EVENT_DECISION]
        if switched_on:
            assert len(sink.sent) == 1 and sink.sent[0].assignee == "u1" and len(asks) == 1
            assert asks[0]["data"]["question"] == "What are the trip dates?"
            result = [e for e in events if e["type"] == EVENT_RESULT][-1]
            assert "What are the trip dates?" in result["text"]
        else:
            assert not sink.sent and not asks


def test_a_worker_claim_with_figures_and_no_receipt_never_reaches_an_interim():
    claim = "Lenk 2025 raised VO2max by +4.1 mL/kg/min at 2x per week"
    verdict = dict(NOT_MET, reason="the table row for " + claim + " is unverified", next_action="check " + claim)
    provider = Provider([dict(ANALYSIS, work_shape="action")], verdicts=[dict(verdict), {"met": True}])
    runner = Runner([DeepResult(met=True, output=claim), DeepResult(met=True, output="all")])
    events = run(build(provider, runner, interim_replies=True, min_seconds_between_interims=0.0))
    assert not [e for e in events if e["type"] == EVENT_INTERIM]   # no receipt: nothing to show


def test_the_interim_carries_only_receipts_and_the_verifiers_request_level_gap():
    claim = "VO2max +4.1 mL/kg/min"
    verdict = dict(NOT_MET, reason="the row says " + claim, next_action="verify " + claim)
    provider = Provider([dict(ANALYSIS, work_shape="action")], verdicts=[dict(verdict), {"met": True}])
    runner = Runner([receipts("worker says " + claim, "Opened 2 of 6 papers"), receipts("all", "Opened 6 papers")])
    events = run(build(provider, runner, interim_replies=True, min_seconds_between_interims=0.0))
    data = [e for e in events if e["type"] == EVENT_INTERIM][0]["data"]
    assert "Opened 2 of 6 papers" in data["done"]
    assert all(claim not in str(data[k]) for k in ("done", "why_more", "next"))
    assert data["why_more"] == "two entries are still missing"


def test_a_flagged_claim_or_a_verdict_without_the_request_level_gap_sends_no_interim():
    for verdict in (dict(NOT_MET, claims_unexecuted=True), {k: v for k, v in NOT_MET.items() if not k.startswith("progress")}):
        provider = Provider([dict(ANALYSIS, work_shape="action")], verdicts=[dict(verdict), {"met": True}])
        runner = Runner([receipts("x", "Opened 2 papers"), receipts("y", "Opened 3 papers")])
        events = run(build(provider, runner, interim_replies=True, min_seconds_between_interims=0.0))
        assert not [e for e in events if e["type"] == EVENT_INTERIM]


def test_why_only_you_is_written_from_the_assignee_never_from_the_verifier_reason():
    from quest_ai_runner.core.ask_policy import Assignee, why_only_you_of
    people = [{"user_id": "u2", "name": "Bram"}]
    assert why_only_you_of("needs_person", Assignee("u2", "member"), people) == "Only Bram can answer this."
    assert why_only_you_of("needs_person", Assignee("u1", "asker"), people) == ""
    assert why_only_you_of("tool_failure", Assignee("u1", "asker"), people).startswith("A tool refused access")
    verdict = {"met": False, "reason": "the Quest API was unreachable", "blocker": "needs_person", "asked_of": "member",
               "member_ref": "u2", "question": "May the deposit be released?"}
    sink = Sink()
    orch = build(Provider([dict(ANALYSIS, work_shape="action")], verdicts=[verdict]), Runner([receipts("p", "Read")]), sink=sink,
                 interim_replies=True)
    run(orch, meta={"asker_user_id": "u1", "conv_id": "c", "turn_id": "t",
                    "participants": [{"user_id": "u1", "name": "Ana"}, {"user_id": "u2", "name": "Bram"}]})
    assert sink.sent[0].why_only_you == "Only Bram can answer this." and "unreachable" not in sink.sent[0].why_only_you


def test_a_final_never_carries_the_machine_folder_the_worker_wrote_in():
    from quest_ai_runner.core.path_pass import relativize
    root = "/srv/qar/run-7"
    assert relativize(f"I read {root}/.quest-context/club.json and {root}/notes.md.", [root]) == "I read .quest-context/club.json and notes.md."
    assert relativize(f"saved in {root}", [root]) == "saved in the working folder"
    assert relativize("see /tmp/x/y", ["/tmp", "/"]) == "see /tmp/x/y"   # a shallow root is never hidden
    provider = Provider([dict(ANALYSIS, work_shape="action")], verdicts=[{"met": True}])
    orch = build(provider, Runner([DeepResult(met=True, output=f"Done. The file is {root}/out/report.md")]), private_path_roots=[root])
    result = orch.run("do the thing")
    texts = " ".join(str(d.output) for d in result.deep_results) + str(result.text or "")
    assert root not in texts and "out/report.md" in texts
