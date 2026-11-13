"""The read-only deep search the names pass may use as its last route: a worker told which name nothing placed and sent to look through the corpus, repositories and files.

It reuses the deep-run machinery the orchestrator already has (the same call the unmet-goal pursuit makes) and returns only structured facts: whether the run's own goal check
said met, and what it produced. Nothing here reads the words of that output."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Mapping

from quest_ai_runner.the_body.internal_context.adapters_values import PlanDecision
from quest_ai_runner.the_intelligence.critical_thinking.unmet_goal import READ_ONLY_BRIEF
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import _truncate_goal


def names_deep_search(orchestrator: Any, rs: SimpleNamespace, open_names: List[str]) -> Mapping[str, Any]:
    named = ", ".join(open_names)
    plan = PlanDecision(
        action="deep", goal=_truncate_goal(f"Find what {named} stands for, from the files and repositories on this machine"),
        deep_brief=(f"{rs.user_message}\n\nNOTE: no glossary and no quick file search placed what {named} stands for. Look through the repositories, READMEs, CLAUDE.md files and docs "
                    f"for where it is defined and report its expansion and what it is, naming the file. {READ_ONLY_BRIEF}")[:2000],
        rationale="names pass: the last route before asking the person",
        deep_difficulty=getattr(rs.plan, "deep_difficulty", None), deep_difficulty_reason=getattr(rs.plan, "deep_difficulty_reason", None))
    res = orchestrator._run_deep(
        plan, rs.user_message, orchestrator._answer_model(plan, "balanced", hint=rs.model_hint), emit=rs.emit, rep_preamble=rs.rep_preamble, exec_record=rs.exec_record,
        gathered=rs.gathered, quality_standards=rs.quality_standards, pending_inputs=rs.pending_inputs, model_hint=rs.model_hint, ctx_meta=rs._ctx_meta,
        cancel_check=rs.cancel_check, working_dir_override=rs.working_dir_override, resume_session_id=rs.take_resume_session(), self_initiated=True,
        user_ordered=False, run_control=rs.run_control, card_context=rs.card_context)
    results = list(getattr(res, "deep_results", None) or [])
    text = "\n\n".join(str(getattr(d, "output", "") or "") for d in results).strip()
    return {"met": bool(results) and all(getattr(d, "met", False) for d in results), "text": text}
