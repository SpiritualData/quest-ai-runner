"""The deep prompt budget: one explicit budget, spent by priority, and nothing that accumulates.

Incident (2026-10-06): autopilot work threads failed with "The deep worker could not start: Prompt
is too long". One daily pass's prompt had grown 367K -> 548K -> 838K characters over three passes:
retrieved past conversations quoted whole earlier briefs (sixteen context-updates blocks in one
prompt), the request rode twice, the thread resumed a transcript holding every earlier pass, and
an explicit "opus" deep model ran on haiku. Each fix is pinned here, and the last test runs several
consecutive passes on one work thread end to end and checks the prompt stays bounded.

Fully offline: stub providers, capturing deep runners, an intercepted ``subprocess.Popen``.
"""
from __future__ import annotations

import json
import subprocess as _sp
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from quest_ai_runner.core import goal_runner, prompt_budget
from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.core.goal_runner import (SubprocessConfig, SubprocessGoalRunner,
                                              compose_goal_prompt)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.core.prompt_budget import (PRIORITY_HISTORY, PRIORITY_REQUEST,
                                                PRIORITY_RETRIEVAL, PRIORITY_UPDATES, Section,
                                                count_blocks, estimate_tokens, fit_sections)
from quest_ai_runner.core.turn_context_store import TurnContextStore
from quest_ai_runner.runner.autopilot import compose_batch_text, render_last_run_output
from quest_ai_runner.runner.context_updates import (BLOCK_END, BLOCK_START, parse_manifest,
                                                    usage_receipt_gate)
from quest_ai_runner.runner.executor import TaskExecutor, across_pass_resume, task_scope_tags

from .conftest import StubProvider, StubRetrieval
from .test_deep_model_pin import PLAN, RecordingRunner, ScriptedProvider
from .test_runner import MockQuestClient


def updates_block(tag: str, size: int = 20_000, refs=("U1", "U2")) -> str:
    """A context-updates block shaped like ``ContextUpdates.as_prompt_block`` writes it."""
    lines = [BLOCK_START, "What changed since an assistant last looked:", ""]
    lines += [f"[{r}] note {tag} {r}" for r in refs]
    lines.append("")
    lines.append(f"[{refs[0]}] body {tag} " + ("x" * size))
    lines.append(BLOCK_END)
    return "\n".join(lines) + "\n\n" + usage_receipt_gate(list(refs))


# --- the fitting mechanism ----------------------------------------------------------------------

def test_under_budget_nothing_changes():
    sections = [Section("a", "alpha", PRIORITY_REQUEST, required=True),
                Section("b", "beta", PRIORITY_HISTORY)]
    fit = fit_sections(sections, 1000)
    assert [s.text for s in fit.sections] == ["alpha", "beta"]
    assert fit.cuts == []


def test_lowest_priority_goes_first_and_the_request_is_kept():
    request = Section("request", "R" * 4000, PRIORITY_REQUEST, required=True)
    updates = Section("updates", "U" * 4000, PRIORITY_UPDATES)
    history = Section("history", "H" * 4000, PRIORITY_HISTORY, drop_note="[history left out]")
    cards = Section("cards", "C" * 4000, PRIORITY_RETRIEVAL)
    budget = estimate_tokens("R" * 4000) + estimate_tokens("U" * 4000) + 200

    fit = fit_sections([request, updates, history, cards], budget)

    texts = {s.name: s.text for s in fit.sections}
    assert texts["request"] == "R" * 4000, "the request is never cut while anything else is left"
    assert texts["updates"] == "U" * 4000, "fresh updates outrank history and cards"
    assert texts["cards"] == "", "retrieval cards go first"
    assert texts["history"] in ("[history left out]",) or len(texts["history"]) < 4000
    assert fit.tokens_after <= budget
    assert [s.name for s in fit.sections] == ["request", "updates", "history", "cards"], \
        "order is never changed"
    assert {c.name for c in fit.cuts} >= {"cards"}


def test_the_request_is_clipped_only_as_the_last_resort():
    fit = fit_sections([Section("request", "R" * 40_000, PRIORITY_REQUEST, required=True)], 2_000)
    assert fit.tokens_after <= 2_000
    assert "cut here to fit this run's prompt budget" in fit.sections[0].text


def test_the_budget_never_exceeds_what_the_model_window_holds(monkeypatch):
    monkeypatch.setenv(prompt_budget.DEEP_PROMPT_BUDGET_ENV, "5000000")
    assert prompt_budget.resolve_deep_prompt_budget(model="opus") == \
        prompt_budget.max_prompt_tokens_for("opus") < prompt_budget.DEFAULT_CONTEXT_WINDOW_TOKENS
    monkeypatch.setenv(prompt_budget.DEEP_PROMPT_BUDGET_ENV, "20000")
    assert prompt_budget.resolve_deep_prompt_budget(model="opus") == 20_000
    assert prompt_budget.resolve_deep_prompt_budget(30_000, model="opus") == 30_000
    monkeypatch.delenv(prompt_budget.DEEP_PROMPT_BUDGET_ENV)
    assert prompt_budget.resolve_deep_prompt_budget() == \
        prompt_budget.DEFAULT_DEEP_PROMPT_TOKEN_BUDGET


# --- one context-updates block per prompt -------------------------------------------------------

def test_a_prompt_keeps_exactly_one_updates_block_and_it_is_the_newest():
    preamble = ("=== CONTEXT DOCTRINE (applies to this run) ===\nthink\n=== END DOCTRINE ===\n\n"
                "--- RELEVANT PAST CONVERSATIONS ---\n" + updates_block("old-1", 500))
    brief = updates_block("stale-standing", 500) + "\n\nThis is the scheduled run.\n" + \
        updates_block("today", 500)

    prompt = compose_goal_prompt("do it", brief, preamble=preamble)

    assert count_blocks(prompt) == 1
    assert "body today" in prompt
    assert "body old-1" not in prompt and "body stale-standing" not in prompt
    assert prompt.count("BEFORE YOU FINISH, account for the context updates") == 1, \
        "a removed block takes its receipt gate with it"


def test_the_receipt_manifest_is_read_from_the_newest_block():
    text = updates_block("stale", 10, refs=("U1",)) + "\n\n" + updates_block("today", 10,
                                                                            refs=("U1", "U2"))
    manifest = parse_manifest(text)
    assert any("today" in line for line in manifest)
    assert not any("stale" in line for line in manifest)


def test_an_earlier_output_never_carries_its_block_into_the_next_brief():
    rendered = render_last_run_output({"result": "the plan\n\n" + updates_block("old", 50),
                                       "status": "done", "worked_at": "2026-10-05T10:00:00Z"})
    assert BLOCK_START not in rendered and "the plan" in rendered


def test_under_budget_the_deep_prompt_is_byte_identical():
    preamble = "Org context.\n\n--- RELEVANT PAST CONVERSATIONS ---\n[2026-10-01] User: hi"
    prompt = compose_goal_prompt("the goal", "the brief", preamble=preamble)
    assert prompt.startswith(preamble + "\n\nTASK:\nthe brief\n\nGOAL (the done-standard")


def test_over_budget_history_is_cut_before_the_task():
    preamble = ("Org context.\n\n--- RELEVANT PAST CONVERSATIONS ---\n" + ("p" * 200_000)
                + "\n\n=== AI REP CONTEXT (what this rep tends to look at) ===\n" + ("c" * 50_000))
    brief = "B" * 100_000
    prompt = compose_goal_prompt("goal", brief, preamble=preamble, budget_tokens=40_000)
    assert estimate_tokens(prompt) <= 40_000
    assert brief in prompt, "today's request survives whole"
    assert "Org context." in prompt


# --- past turns: clipped, and fenced to their quest ---------------------------------------------

def test_a_past_turn_is_a_pointer_not_a_copy(tmp_path):
    store = TurnContextStore(turns_dir=str(tmp_path / "turns"))
    big = "Act as Zee. Quest outcome: funding.\n\n" + updates_block("x", 30_000) + ("y " * 20_000)
    store.record(big, {"response": "z" * 5_000, "scope_tags": ["quest:q1"]})

    view = store.assemble("funding Zee", meta={"scope_tags": ["quest:q1"]}).context_view

    assert len(view) < 1_500
    assert BLOCK_START not in view
    card = json.loads(next((tmp_path / "turns").glob("*.json")).read_text())
    assert len(card["user"]) <= 4_100 and card["scope_tags"] == ["quest:q1"]


def test_a_turn_from_another_quest_never_surfaces(tmp_path):
    store = TurnContextStore(turns_dir=str(tmp_path / "turns"))
    store.record("paid subscriber KPI work", {"response": "kpi", "scope_tags": ["quest:other"]})
    store.record("funding hour plan", {"response": "plan", "scope_tags": ["quest:q1"]})

    view = store.assemble("paid subscriber funding plan",
                          meta={"scope_tags": ["quest:q1"]}).context_view

    assert "funding hour plan" in view and "subscriber KPI" not in view


def test_a_quest_task_is_scoped_by_its_goal_id():
    """A task created against a quest carries the quest in goal_id, quest_id empty."""
    assert task_scope_tags({"goal_id": "quest_a", "quest_id": None}) == ["quest:quest_a"]
    assert task_scope_tags({}) == []


# --- session resume -----------------------------------------------------------------------------

def test_an_autopilot_work_thread_starts_each_pass_fresh():
    work = {"task_id": "t", "task_kind": "autopilot_work", "limit_hit_count": 0}
    assert across_pass_resume(work, "sess-yesterday") is None
    paused = dict(work, limit_hit_count=1)
    assert across_pass_resume(paused, "sess-today") == "sess-today", \
        "a pass paused on the usage limit continues its own session"
    assert across_pass_resume({"task_id": "r"}, "sess-reply") == "sess-reply", \
        "an ordinary thread still resumes"


def envelope(result: str, *, is_error: bool = False, tokens: int = 5) -> bytes:
    return json.dumps({"type": "result", "subtype": "success", "is_error": is_error,
                       "result": result, "usage": {"input_tokens": tokens, "output_tokens": 0},
                       "total_cost_usd": 0.0}).encode()


def fake_worker(monkeypatch, replies):
    """Intercept the worker: each launch gets the next ``(returncode, stdout)`` and is recorded."""
    calls: List[Dict[str, Any]] = []

    class Proc:
        stdin = None

        def __init__(self, cmd):
            self.returncode, self._out = replies[min(len(calls) - 1, len(replies) - 1)]

        def communicate(self, input=None, timeout=None):
            calls[-1]["prompt"] = (input or b"").decode()
            return (self._out, b"")

    def popen(cmd, **kw):
        calls.append({"cmd": list(cmd)})
        return Proc(cmd)

    monkeypatch.setattr(_sp, "Popen", popen)
    return calls


def test_a_transcript_too_large_to_replay_is_not_resumed(monkeypatch, tmp_path):
    session = tmp_path / "sess-huge.jsonl"
    with open(session, "w") as fh:
        for _ in range(60):
            fh.write(json.dumps({"type": "user", "message": {"content": "p" * 20_000}}) + "\n")
    monkeypatch.setattr(goal_runner, "resolve_session_file", lambda wd, sid: session)
    calls = fake_worker(monkeypatch, [(0, envelope("done"))])
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir=str(tmp_path),
                                                   claude_path="/usr/bin/claude"))

    res = runner.run_goal(goal="g", brief="b", model="opus", resume_session_id="sess-huge")

    assert res.met is True
    assert "--resume" not in calls[0]["cmd"] and "--session-id" in calls[0]["cmd"]


def test_a_small_transcript_is_still_resumed(monkeypatch, tmp_path):
    session = tmp_path / "sess-small.jsonl"
    session.write_text(json.dumps({"type": "user", "message": {"content": "hello"}}) + "\n")
    monkeypatch.setattr(goal_runner, "resolve_session_file", lambda wd, sid: session)
    calls = fake_worker(monkeypatch, [(0, envelope("done"))])
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir=str(tmp_path),
                                                   claude_path="/usr/bin/claude"))

    runner.run_goal(goal="g", brief="b", model="opus", resume_session_id="sess-small")

    assert "--resume" in calls[0]["cmd"]


def test_prompt_too_long_retries_fresh_then_tighter_then_says_why(monkeypatch, tmp_path):
    monkeypatch.setattr(goal_runner, "resolve_session_file", lambda wd, sid: None)
    too_long = (1, envelope("Prompt is too long", is_error=True, tokens=0))
    calls = fake_worker(monkeypatch, [too_long])
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir=str(tmp_path),
                                                   claude_path="/usr/bin/claude",
                                                   prompt_token_budget=40_000))
    preamble = "--- RELEVANT PAST CONVERSATIONS ---\n" + ("p" * 400_000)

    res = runner.run_goal(goal="g", brief="the request", model="opus",
                          context_preamble=preamble, resume_session_id="sess-big")

    assert "--resume" in calls[0]["cmd"]
    assert all("--resume" not in c["cmd"] for c in calls[1:]), "the retry drops the transcript"
    sizes = [len(c["prompt"]) for c in calls]
    assert sizes[-1] < sizes[1], "each further retry is fitted to a tighter budget"
    assert res.launch_failed is True
    assert "too long for its context window" in res.error
    assert "Nothing was done in this run" in res.error


# --- the deep model the task asked for ----------------------------------------------------------

def test_an_opus_request_is_not_run_on_a_lanes_cheap_quality_tier():
    """The SD shared lane maps every shallow tier to haiku; a thread asking for opus got haiku."""
    provider = ScriptedProvider(plans=[PLAN], verdicts=[{"met": True}])
    runner = RecordingRunner()
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider, fallback={"quality": "haiku",
                                                                   "best": "haiku"}),
                        deep_runner=runner, config=OrchestratorConfig())

    orch.run("write the hour plan", model_hint="opus")

    assert [c["model"] for c in runner.calls] == ["opus"]


def test_a_family_request_that_resolves_within_its_family_keeps_the_lanes_version():
    provider = ScriptedProvider(plans=[], verdicts=[])
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), deep_runner=RecordingRunner(),
                        config=OrchestratorConfig())
    assert orch.resolve_deep_ladder("opus", None, "claude-opus-4-8") == (["claude-opus-4-8"], True)
    assert orch.resolve_deep_ladder("opus", None, "haiku") == (["opus"], True)


# --- several consecutive passes on one work thread ----------------------------------------------

class CapturingRunner:
    """A deep runner that records what it was handed and composes the real worker prompt."""

    def __init__(self, result: str):
        self.result = result
        self.prompts: List[str] = []
        self.resumes: List[Optional[str]] = []

    def run_goal(self, *, goal, brief, model=None, max_turns=None, context_preamble=None,
                 working_dir=None, resume_session_id=None) -> DeepResult:
        self.prompts.append(compose_goal_prompt(goal, brief, preamble=context_preamble or "",
                                                model=model))
        self.resumes.append(resume_session_id)
        return DeepResult(met=True, output=self.result, session_id="sess-pass")


def compose_pass(day: int, last_result: Optional[str]) -> str:
    return compose_batch_text(
        "Successful execution of the funding round", "Zee (AI)", scope_label="week:2026_W40",
        instructions="Write Joshua's hour plan: an ordered list he follows top to bottom. " * 40,
        goal_ladder=[{"scope": "week", "period": "2026_W40",
                      "goals": [{"name": f"Grant {i}", "deadline": "2026-10-09"}
                                for i in range(8)]}],
        next_steps="## Next steps\n" + "\n".join(f"{i}. step {i}" for i in range(30)),
        context_updates=updates_block(f"day{day}", 20_000, refs=tuple(f"U{i}" for i in
                                                                      range(1, 21))),
        last_run=(render_last_run_output({"result": last_result, "status": "done",
                                          "worked_at": f"2026-10-0{day}T19:00:00Z"})
                  if last_result else None),
        previous={"period": f"day:2026-10-0{day}",
                  "tasks": [{"title": "Hour plan", "status": "done", "result": "plan " * 200}]})


def test_consecutive_passes_on_one_thread_stay_bounded(tmp_path):
    """Six daily passes on one autopilot work thread, the way prod ran them: each pass's text is
    the thread's legacy standing text (pass 1's whole brief), the backend's earlier-run excerpts
    and the new brief; every finished turn is recorded into the same turn store, alongside turns
    from another quest the same persona works; and the thread hands back its last session each
    time. The prompt must stay inside its budget, hold exactly one context-updates block, not grow
    from pass to pass, and never carry the other quest's work."""
    turns = TurnContextStore(turns_dir=str(tmp_path / "turns"))
    turns.record("Work the paid-subscriber KPI as Zee's representative " * 200,
                 {"response": "KPI pass", "scope_tags": ["quest:quest_other"]})
    standing: Optional[str] = None
    last_result: Optional[str] = None
    history: List[str] = []
    sizes: List[int] = []
    for day in range(1, 7):
        brief = compose_pass(day, last_result)
        if standing is None:
            # Pass 1 creates the thread; before the backend fix its standing_text froze this
            # whole brief and every later run's text was composed from it.
            standing = text = brief
        else:
            text = standing + "\n\nWhat earlier runs on this same thread reported, newest " \
                "first:\n" + "\n".join(history[:3])
            text += f"\n\nThis is the scheduled run for 2026-10-0{day}.\n" + brief
        result = (f"**Day {day} hour plan.**\n" + "1. Do the thing. 10 min.\n" * 80
                  + "\nContext used:\n" + "\n".join(f"  [U{i}] not used" for i in range(1, 21)))
        runner = CapturingRunner(result)
        provider = StubProvider(decisions=[
            {"action": "deep", "goal": "Write today's hour plan", "rationale": "work"},
            {"met": True, "reason": "did it"},
        ])
        brain = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                             registry=ModelRegistry(provider), deep_runner=runner,
                             context_assembler=turns)
        TaskExecutor(MockQuestClient([]), brain).execute({
            "id": f"atask_{day}", "task_id": "atask_thread", "text": text,
            "goal_id": "quest_funding", "task_kind": "autopilot_work",
            "resume_session_id": "sess-pass" if day > 1 else None, "limit_hit_count": 0})

        prompt = runner.prompts[0]
        sizes.append(estimate_tokens(prompt))
        assert count_blocks(prompt) == 1, f"pass {day}: one context-updates block"
        assert f"body day{day}" in prompt, f"pass {day}: and it is today's"
        assert prompt.count(f"body day{day}") == 1, f"pass {day}: today's updates appear once"
        if day > 1:
            assert prompt.count(f"This is the scheduled run for 2026-10-0{day}") == 1, \
                f"pass {day}: the request is in the prompt once, not twice"
        assert "paid-subscriber KPI" not in prompt, f"pass {day}: another quest's turn leaked"
        assert runner.resumes == [None], f"pass {day}: a work thread starts each pass fresh"
        assert sizes[-1] <= prompt_budget.DEFAULT_DEEP_PROMPT_TOKEN_BUDGET
        history.insert(0, f"Run {day}, status done:\n{result[:1500]}\n[trimmed]")
        last_result = result

    assert max(sizes[2:]) - min(sizes[2:]) < 0.1 * max(sizes), \
        f"the prompt keeps growing across passes: {sizes}"


# --- what an autopilot run accounts for is recorded, so it is not re-offered forever ------------

def test_an_autopilot_runs_receipt_closes_what_it_accounts_for(tmp_path):
    """The pass composes the brief; the executor runs it later holding only the task. The run's
    receipt used to be dropped (no bundle on the executor), so every item it finished came back
    "still owed" on every later pass: 18 of 20 refs in the incident's block."""
    from quest_ai_runner.runner.feedback_ledger import FeedbackLedger

    ledger = FeedbackLedger(str(tmp_path / "feedback.json"))
    ledger.observe(card_id="quest_a", source="quest_notes", item_id="n1",
                   text="Please add the Status column")
    assert [i.item_id for i in ledger.open_items("quest_a")] == ["n1"]
    ledger.remember_offer("atask_thread", "quest_a", [("U1", "quest_notes", "n1")])

    class Engine:
        _ledger = FeedbackLedger(str(tmp_path / "feedback.json"))   # another process's view

    executor = TaskExecutor(MockQuestClient([]), None, update_engine=Engine())
    executor._receipt_task_id = "atask_thread"
    brief = "Work this.\n\n" + updates_block("today", 10, refs=("U1",))
    executor._with_context_receipt("Added it.\n\nContext used:\n  [U1] done: added the column",
                                   brief, autopilot_composed=True)

    assert FeedbackLedger(str(tmp_path / "feedback.json")).open_items("quest_a") == []


# --- one history per prompt, and never the thread's own earlier runs ----------------------------

def test_only_one_turn_history_is_rendered():
    from quest_ai_runner.core.composite_assembler import CompositeContextAssembler
    from quest_ai_runner.core.turn_context_store import assembler_renders_turns

    class Hybrid:
        def __init__(self, keyword):
            self._keyword, self._vector = keyword, object()

    lane = Hybrid(CompositeContextAssembler([object(), TurnContextStore(turns_dir="/nonexistent")]))
    assert assembler_renders_turns(lane) is True
    assert assembler_renders_turns(Hybrid(object())) is False
    assert assembler_renders_turns(None) is False


def test_a_threads_own_earlier_runs_are_not_retrieved_as_past_conversations(tmp_path):
    store = TurnContextStore(turns_dir=str(tmp_path / "turns"))
    store.record("hour plan for Friday", {"response": "Friday plan", "task_id": "atask_thread",
                                          "scope_tags": ["quest:q1"]})
    store.record("grant review hour plan", {"response": "grant notes", "task_id": "atask_other",
                                            "scope_tags": ["quest:q1"]})

    view = store.assemble("hour plan", meta={"scope_tags": ["quest:q1"],
                                             "task_id": "atask_thread"}).context_view

    assert "grant review" in view and "Friday plan" not in view


# --- judges get the gist of a request, not the whole brief --------------------------------------

def test_a_relevance_judge_gets_the_gist_not_the_whole_brief():
    from quest_ai_runner.core.card_filter import filter_cards_by_relevance

    seen: List[str] = []

    class Provider:
        def answer(self, messages, model=None, **kw):
            seen.append(messages[-1]["content"])
            return '{"cards": [{"id": "c1", "score": 0.9}]}'

    brief = "Act as Zee. " + updates_block("x", 30_000) + ("middle " * 20_000) + "Today: plan."
    filter_cards_by_relevance(brief, [{"id": "c1", "title": "plan", "files": []}],
                              model_provider=Provider(), model="haiku")

    assert seen and all(len(p) < 8_000 for p in seen)
    assert "Act as Zee." in seen[0] and "Today: plan." in seen[0]
