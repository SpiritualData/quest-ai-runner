"""The reply check (the hub's Decision Check on a chat reply), offline with a scripted cheap-tier model: a claim of a change the record does not hold is removed alone."""
import json
from types import SimpleNamespace

from quest_ai_runner.the_intelligence.critical_thinking.decision_check import reply_views as V

REPLY = "I read the plan. I added the weekly task to the quest. The deadline is on the plan."
EXECUTED = "Changes made this turn:\n- tool create_note: a note on the plan"


def scripted(replies):
    seen = []

    def ask(prompt):
        seen.append(prompt)
        for key, answer in replies.items():
            if key in prompt:
                return json.dumps(answer)
        return json.dumps({"findings": []})
    ask.seen = seen
    return ask


def turn(enabled=True):
    cfg = SimpleNamespace(reply_check=enabled, reply_check_tier="fast", reply_check_max_calls=6)
    return SimpleNamespace(cfg=cfg), SimpleNamespace(gathered=[{"locator": "doc", "text": "The plan: deadline is Friday."}], exec_record=None, _ctx_meta={"participants": [{"name": "Sam"}]})


def test_the_binding_gives_each_view_only_its_own_evidence():
    b = V.reply_binding("plan text", EXECUTED, "the person writing to us, Sam")
    evidence = {v["id"]: v["evidence"] for v in b["views"]}
    assert "plan text" in evidence["reads"] and "create_note" not in evidence["reads"]
    assert "create_note" in evidence["actions"] and "plan text" not in evidence["actions"]
    assert "Sam" in evidence["asks"] and "create_note" not in evidence["asks"] and b["max_calls"] == 40
    # Every factual sentence is held to the reads alone (a missed review nobody read is not a fact the reply may state).
    assert "plan text" in evidence["facts"] and "create_note" not in evidence["facts"] and "Sam" not in evidence["facts"]


def test_an_action_claimed_but_not_executed_is_removed_and_the_rest_of_the_reply_stays(monkeypatch):
    ask = scripted({"Rewrite only the failing part": {"action": "remove", "text": ""},
                    "For each numbered statement": {"verdicts": [{"n": 1, "fails": True, "quote": ""}]},
                    "List each part of the reply": {"findings": [{"span": "I added the weekly task to the quest.", "quote": ""}]}})
    monkeypatch.setattr(V, "model_ask", lambda orch, tier: ask)
    monkeypatch.setattr(V, "tool_landed_account", lambda record: EXECUTED, raising=False)
    orch, rs = turn()
    out = V.checked_reply(orch, rs, REPLY)
    assert out == "I read the plan. The deadline is on the plan." and rs.reply_check["outcome"] == "repaired"


def test_the_check_is_off_by_default_and_a_failing_model_sends_the_reply_as_it_was(monkeypatch):
    orch, rs = turn(enabled=False)
    assert V.checked_reply(orch, rs, REPLY) == REPLY

    def broken(o, tier):
        raise RuntimeError("down")
    monkeypatch.setattr(V, "model_ask", broken)
    orch, rs = turn()
    assert V.checked_reply(orch, rs, REPLY) == REPLY


def test_a_failed_chat_evaluation_case_becomes_a_mistake_of_the_view_its_recorded_facts_name():
    from quest_ai_runner.the_intelligence.critical_thinking.decision_check.mistake_sources import eval_failure_mistakes
    rows = [{"case_id": "A", "passed": False, "reply": "Total is 300.", "reason": "figures never read", "unsupported_figures": ["300"], "reads_seen": [{"text": "revenue page"}]},
            {"case_id": "B", "passed": False, "reply": "Nothing changed.", "reason": "it did change", "unexpected_writes": ["goal added"], "changes": ["goal added x"]},
            {"case_id": "C", "passed": False, "reply": "Rude.", "reason": "tone"},
            {"case_id": "D", "passed": True, "reply": "ok", "unsupported_figures": ["1"]}]
    got = eval_failure_mistakes(rows, "2026-11-08")
    assert [(m.check_id, m.case_id, m.source) for m in got] == [("reads", "A", "eval_failure"), ("actions", "B", "eval_failure")]
    assert got[0].input_slice == "revenue page" and got[1].input_slice == "goal added x" and got[1].right == "it did change"


def test_the_reads_and_facts_views_see_the_context_the_reply_was_written_from(monkeypatch):
    # A true sentence taken from the turn's context (its status snapshot, the quest's own state) was removed as unsupported when the views saw only the reads.
    ask = scripted({})
    monkeypatch.setattr(V, "model_ask", lambda orch, tier: ask)
    orch, rs = turn()
    rs.context_view = "Quest state: the store release is on the test track only."
    V.checked_reply(orch, rs, "The store release is on the test track only.")
    facts_prompts = [p for p in ask.seen if "A statement of fact about" in p]
    assert facts_prompts and "the store release is on the test track only" in facts_prompts[0]


def test_a_confirmed_span_removed_instead_of_rewritten_is_kept_as_a_mistake_record(tmp_path):
    from hubofhubs.api import MistakeLog
    rows = [{"id": "m1", "check_id": "facts", "input_slice": "reads", "wrong": "A claim nobody read.", "right": "the repair removed it", "source": "confirmed_failure",
             "case_id": "", "at": ""}]
    V.keep_mistakes(rows, str(tmp_path / "mistakes.jsonl"))
    V.keep_mistakes(rows, str(tmp_path / "mistakes.jsonl"))
    kept = MistakeLog(tmp_path / "mistakes.jsonl").read("facts")
    assert [m.wrong for m in kept] == ["A claim nobody read."]
