"""Quest operations as registry tools: each defined once, shown to a run only when its work calls for it.

Two things are pinned here. (1) Each operation behaves the way a run needs: the quest comes from the
run, an outcome or an ask is found by its words, a duplicate ask is recognised, a refusal says how to
fix the call. (2) The context test: a run whose work involves an operation gets that operation's
how-to in its deep brief, and an unrelated run gets none of it.
"""
import pytest

from .conftest import StubDeepRunner, StubProvider
from .test_tools import make_orch
from quest_ai_runner.core.tools import ToolContext, ToolRegistry, build_tool_registry
from quest_ai_runner.core import tools_quest_ops as ops
from quest_ai_runner.runner.executor_support import AUTOPILOT_WORK_KIND
from quest_ai_runner.core.tools_quest_work import AUTOPILOT_WORK_PINS
from quest_ai_runner.runner.run_operations import pinned_operations

@pytest.fixture(autouse=True)
def standard_tools_on(monkeypatch):
    monkeypatch.delenv("QAR_STANDARD_TOOLS", raising=False)


CREDS = {"QUEST_BASE_URL": "https://quest.example", "QUEST_API_KEY": "key"}
OUTCOMES = [{"id": "out_a", "text": "Reach 1000 newsletter subscribers"},
            {"id": "out_b", "text": "Sign 5 partner organisations"}]


class FakeClient:
    """Records every call; answers like QuestClient (a refusal is an empty dict)."""

    def __init__(self, asks=(), decisions=None):
        self.calls = []
        self.asks = asks
        self.decisions = decisions or []
        self.accept = True

    def list_open_decisions_for_quest(self, quest_id):
        return list(self.decisions)

    def create_decision(self, summary, **kw):
        self.calls.append(("create_decision", summary, kw))
        return {"decision_id": "dec_1"} if self.accept else {}

    def list_asks(self, *, quest_id, limit=50):
        return self.asks

    def record_ask(self, quest_id, text, **kw):
        self.calls.append(("record_ask", quest_id, text, kw))
        return {"key": "k_new", "state": "open", "text": text}

    def update_ask(self, item_key, state=None, note="", kind=None):
        self.calls.append(("update_ask", item_key, state, note, kind))
        return {"key": item_key, "state": "awaiting_acceptance" if state == "done" else state}

    def update_outcome_progress(self, quest_id, outcome_id, progress_pct, note, current_value=None):
        self.calls.append(("progress", quest_id, outcome_id, progress_pct, note, current_value))
        return {"id": outcome_id, "text": "Reach 1000 newsletter subscribers",
                "progress_pct": progress_pct}

    def resolve_outcome_id(self, quest_id, outcome):
        for o in OUTCOMES:
            if outcome in (o["id"], "1") or outcome.lower() in o["text"].lower():
                return o["id"], None
        return None, f"{outcome!r} is not an id or the words of any of this quest's outcomes."

    def create_goal(self, title, **kw):
        self.calls.append(("create_goal", title, kw))
        return {"id": "goal_1"}

    def update_goal(self, goal_id, fields):
        self.calls.append(("update_goal", goal_id, fields))
        return {"id": goal_id}


def run(builder, client, args, quest_id="quest_1"):
    spec = builder(CREDS, client_factory=lambda: client)
    return ToolRegistry([spec]).invoke(spec.name, args, ToolContext(quest_id=quest_id))


# --- the operations -------------------------------------------------------------------------

def test_create_decision_takes_the_quest_from_the_run_and_raises_one_ask():
    client = FakeClient()
    result = run(ops.create_decision_spec, client, {"summary": "Approve the sponsor email to Acme?"})
    assert result.ok and "dec_1" in result.text
    assert client.calls == [("create_decision", "Approve the sponsor email to Acme?",
                             {"kind": "approve", "quest_id": "quest_1"})]


def test_create_decision_does_not_raise_what_is_already_open():
    client = FakeClient(decisions=[{"decision_id": "dec_9", "summary": "Approve the sponsor email to Acme"}])
    result = run(ops.create_decision_spec, client, {"summary": "Approve the sponsor email to Acme?"})
    assert result.ok and "dec_9" in result.text and "not raised again" in result.text
    assert client.calls == []


def test_create_decision_tells_the_ai_when_quest_says_it_is_already_open():
    class Open(FakeClient):
        def create_decision(self, summary, **kw):
            return {"decision_id": "dec_4", "duplicate": True,
                    "message": "Not raised again: the same question is already open as ask dec_4."}

    result = run(ops.create_decision_spec, Open(), {"summary": "Approve the outline note?"})
    assert result.ok and "dec_4" in result.text and "Not raised again" in result.text


def test_create_decision_tells_the_ai_when_the_person_already_passed_on_it():
    class Answered(FakeClient):
        def create_decision(self, summary, **kw):
            return {"duplicate": "answered",
                    "message": "Not asked: the person already passed on this same question."}

    result = run(ops.create_decision_spec, Answered(), {"summary": "Approve the outline note?"})
    assert result.ok and "already passed on" in result.text
    assert "NOT been asked" not in result.text


def test_create_decision_refused_by_quest_says_the_person_was_not_asked():
    client = FakeClient()
    client.accept = False
    result = run(ops.create_decision_spec, client, {"summary": "Pick a date"})
    assert not result.ok and "NOT been asked" in result.text


def test_list_asks_shows_open_ones_with_keys_and_hides_the_closed():
    client = FakeClient(asks=[
        {"key": "k1", "state": "open", "kind": "request", "text": "Email the board a summary"},
        {"key": "k2", "state": "accepted", "text": "Old finished request"},
    ])
    text = run(ops.list_asks_spec, client, {}).text
    assert "k1 [open request] Email the board a summary" in text and "k2" not in text
    assert "k2" in run(ops.list_asks_spec, client, {"include_closed": True}).text


def test_a_failed_asks_read_is_not_reported_as_no_asks():
    result = run(ops.list_asks_spec, FakeClient(asks=None), {})
    assert not result.ok and "do not assume" in result.text


def test_update_ask_finds_the_ask_by_a_phrase_of_its_words_and_claims_done():
    client = FakeClient(asks=[{"key": "k1", "state": "open", "text": "Email the board a summary"},
                              {"key": "k3", "state": "open", "text": "Draft the grant letter"}])
    result = run(ops.update_ask_spec, client, {"ask": "grant letter", "state": "done",
                                               "note": "Draft saved in the quest folder"})
    assert result.ok and "can accept it" in result.text
    assert client.calls == [("update_ask", "k3", "done", "Draft saved in the quest folder", None)]


def test_update_ask_sorts_an_unsorted_ask_by_kind_without_a_state():
    client = FakeClient(asks=[{"key": "k4", "state": "open", "kind": "unknown", "text": "Always cc Dana"}])
    result = run(ops.update_ask_spec, client, {"ask": "k4", "kind": "standing"})
    assert result.ok and "sorted as standing" in result.text
    assert client.calls == [("update_ask", "k4", None, "", "standing")]
    assert not run(ops.update_ask_spec, client, {"ask": "k4", "kind": "unknown"}).ok
    assert not run(ops.update_ask_spec, client, {"ask": "k4"}).ok


def test_update_ask_lists_the_keys_when_the_phrase_matches_nothing_or_two():
    client = FakeClient(asks=[{"key": "k1", "state": "open", "text": "Email the board"},
                              {"key": "k3", "state": "open", "text": "Email the funder"}])
    result = run(ops.update_ask_spec, client, {"ask": "email", "state": "done"})
    assert not result.ok and "matches several asks" in result.text and "k1" in result.text and "k3" in result.text
    assert client.calls == []


def test_a_run_can_never_accept_an_ask():
    result = run(ops.update_ask_spec, FakeClient(), {"ask": "k1", "state": "accepted"})
    assert not result.ok and "only ever a person's" in result.text


def test_record_ask_records_the_exact_words():
    client = FakeClient()
    result = run(ops.record_ask_spec, client, {"text": "Please send Ana the Q3 numbers", "author": "ana@example.org"})
    assert result.ok and "k_new" in result.text
    assert client.calls == [("record_ask", "quest_1", "Please send Ana the Q3 numbers",
                             {"kind": "request", "author_id": "ana@example.org"})]


def test_progress_passes_the_outcome_by_words_and_reports_a_pending_approval_honestly():
    client = FakeClient()
    result = run(ops.update_outcome_progress_spec, client, {
        "outcome_id": "newsletter subscribers", "progress_pct": 41, "note": "List export shows 412 of 1000"})
    assert result.ok and client.calls[0][:4] == ("progress", "quest_1", "newsletter subscribers", 41)

    class Pending(FakeClient):
        def update_outcome_progress(self, *a, **k):
            return {"pending_approval": True, "decision_id": "dec_4"}
    pending = run(ops.update_outcome_progress_spec, Pending(),
                  {"outcome_id": "out_a", "progress_pct": 5, "note": "Two partners signed today"})
    assert pending.ok and "waiting for a person" in pending.text and "Do not report" in pending.text


def test_progress_refusal_text_reaches_the_run_verbatim():
    class Refusing(FakeClient):
        def update_outcome_progress(self, *a, **k):
            raise RuntimeError("Not done: a progress note must name concrete evidence")
    result = run(ops.update_outcome_progress_spec, Refusing(),
                 {"outcome_id": "out_a", "progress_pct": 5, "note": "making progress"})
    assert not result.ok and result.text == "Not done: a progress note must name concrete evidence"


def test_create_goal_links_the_outcome_by_its_words():
    client = FakeClient()
    result = run(ops.create_goal_spec, client, {
        "title": "Send the March newsletter", "period": "2026_03", "outcome_id": "partner"})
    assert result.ok
    assert client.calls[0][2]["outcome_id"] == "out_b"
    unknown = run(ops.create_goal_spec, client, {"title": "x", "period": "2026_03", "outcome_id": "zzz"})
    assert not unknown.ok and "not an id" in unknown.text


def test_update_goal_relinks_but_cannot_complete_or_rename():
    client = FakeClient()
    assert run(ops.update_goal_spec, client, {"goal_id": "g1", "outcome_id": "1"}).ok
    assert client.calls[-1] == ("update_goal", "g1", {"outcome_id": "out_a"})
    assert not run(ops.update_goal_spec, client, {"goal_id": "g1", "title": "New name"}).ok


# --- one definition, rendered per mode -------------------------------------------------------

def test_every_operation_is_complete_and_named_after_the_client_method_it_wraps():
    registry = build_tool_registry(CREDS)
    from quest_ai_runner.runner.quest_client import QuestClient
    for name in ops.QUEST_OPERATION_BUILDERS:
        spec = registry.get(name)
        assert spec and spec.when_to_use and spec.when_not_to_use and spec.description, name
        assert not spec.workflow_step, "ledger and progress operations are not workflow steps"
        if name in ("record_ask", "update_ask"):
            continue  # the asks client methods are added by the asks work; the tool checks for them
        assert hasattr(QuestClient, name), f"{name} should match a QuestClient method"


def test_create_goal_is_for_runs_that_ask_for_it_and_never_offered_by_relevance():
    registry = build_tool_registry(CREDS)
    for query in ("create the missing quarter goal for the outcome", "add a goal for next week"):
        assert "create_goal" not in [s.name for s in registry.select(query)]
        assert "create_goal" not in [s.name for s in registry.search(query)]
    assert registry.get("create_goal") is not None            # callable by name
    assert "create_goal" in [s.name for s in registry.select("anything", pins=("create_goal",))]


# --- known only when needed -------------------------------------------------------------------

AUTOPILOT_BATCH = {"id": "atask_pass1", "task_kind": AUTOPILOT_WORK_KIND, "goal_id": "quest_1",
                   "text": "Work this quest today: pick the one or two things that most move it forward."}
PLAIN_TASK = {"id": "atask_plain", "text": "Summarize this PDF of grants for me", "goal_id": "quest_1"}


def deep_brief_for(task, tools):
    from quest_ai_runner.runner.executor import TaskExecutor
    from .test_runner import MockQuestClient

    deep = StubDeepRunner(met=True)
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": task["text"], "deep_brief": "b", "rationale": "work"}])
    TaskExecutor(MockQuestClient([]), make_orch(provider, tools, deep_runner=deep)).execute(task)
    assert deep.calls, "a deep run was expected"
    return deep.calls[0]["brief"]


def test_an_autopilot_batch_is_given_the_asks_and_progress_how_to_without_any_prompt_text():
    brief = deep_brief_for(AUTOPILOT_BATCH, build_tool_registry(CREDS))
    for name in ops.AUTOPILOT_PINS + AUTOPILOT_WORK_PINS:
        assert f"- {name} (standard" in brief, name
    assert "-m quest_ai_runner.tools call" in brief and "--kw" in brief
    assert "- create_goal (" not in brief                    # needs a request; not pinned
    assert "- update_quest_scope (" not in brief             # unrelated field tools stay out


def test_an_unrelated_run_gets_none_of_it():
    brief = deep_brief_for(PLAIN_TASK, build_tool_registry(CREDS))
    assert "TOOLS YOU CAN CALL" not in brief
    for name in ops.QUEST_OPERATION_BUILDERS:
        assert f"- {name} (" not in brief


def test_a_run_that_names_an_ask_in_its_words_finds_the_operation_through_relevance():
    registry = build_tool_registry(CREDS)
    shown = [s.name for s in registry.select("Ana asked us to email the board, track it in the ledger")]
    assert "record_ask" in shown and "list_asks" in shown
    assert registry.render_deep_block("summarize this pdf of grants") == ""


def test_only_autopilot_composed_tasks_are_pinned():
    assert pinned_operations(AUTOPILOT_BATCH) == ops.AUTOPILOT_PINS + AUTOPILOT_WORK_PINS
    pinned = set(pinned_operations(AUTOPILOT_BATCH))
    assert not pinned & {"list_intents", "get_spec", "refresh_spec", "edit_spec_record", "place_quest"}
    assert pinned_operations(PLAIN_TASK) == ()
    # a person pasting a prior brief into chat is not an autopilot batch (task_kind decides)
    assert pinned_operations({"text": "\n".join(["QAR:CONTEXT"] * 3)}) == ()


def test_the_shell_form_takes_plain_kw_arguments(monkeypatch, capsys):
    from quest_ai_runner.tools.__main__ import main
    client = FakeClient()
    monkeypatch.setenv("QUEST_BASE_URL", CREDS["QUEST_BASE_URL"])
    monkeypatch.setenv("QUEST_API_KEY", CREDS["QUEST_API_KEY"])
    from quest_ai_runner.core import tools as core_tools
    monkeypatch.setattr(core_tools, "_quest_client_factory", lambda env: client)
    code = main(["call", "create_decision", "--quest", "quest_1", "--kw", "summary=Approve the 2026 budget?",
                 "--kw", "kind=approve"])
    assert code == 0 and "dec_1" in capsys.readouterr().out
    assert client.calls[0][1] == "Approve the 2026 budget?"
    assert main(["show", "update_outcome_progress"]) == 0
    assert "progress_pct" in capsys.readouterr().out
    assert main(["show", "update_outcom"]) == 1
    assert "update_outcome_progress" in capsys.readouterr().err


def test_a_digit_only_text_argument_stays_text():
    spec = ops.create_decision_spec(CREDS, client_factory=lambda: FakeClient())
    registry = ToolRegistry([spec])
    seen = {}
    spec.handler = lambda args, ctx: seen.update(args) or "ok"
    registry.invoke("create_decision", {"summary": 2026}, ToolContext())
    assert seen["summary"] == "2026"


def test_quest_describe_prints_the_registry_entry_and_suggests_names(capsys):
    from quest_ai_runner import cli
    assert cli.main(["quest", "--describe", "update_ask"]) == 0
    out = capsys.readouterr().out
    assert "USE WHEN" in out and "tools call update_ask" in out
    assert cli.main(["quest", "--describe", "update_outcome_progres"]) == 2
    assert "update_outcome_progress" in capsys.readouterr().err


def test_a_write_that_returns_nothing_is_not_reported_as_success(monkeypatch, capsys):
    from quest_ai_runner import cli, cli_quest

    class Refusing:
        def create_decision(self, summary, **kw):
            return {}
    monkeypatch.setattr(cli_quest, "load_quest_client", lambda config=None: Refusing())
    assert cli.main(["quest", "create_decision", "Approve?", "--write"]) == 1
    assert "NOT done" in capsys.readouterr().err
