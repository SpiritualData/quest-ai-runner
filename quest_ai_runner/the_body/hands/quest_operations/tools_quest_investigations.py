"""Investigations as registry tools: look into a question for a quest, find what was looked into before, and say what a finding changed.

An investigation is one piece of information gathering done to answer a question, on a quest, inside one experiment or on its own, tied to the strategy it
bears on (the quest's own id when nothing narrower fits). Carrying one out is Science's (``the_world/science/investigate.py``, through the hub's Learning
solution); this module is the Quest side: it reads the quest's strategies, experiments and earlier investigations through the Quest API, applies the rules
that keep a quick lookup out of the record, reuses a current earlier investigation instead of searching again, and records the result.

What goes into the record, and what does not, is decided by facts the caller states (``informs``: what the result is for) and by the fast-tier judgements
in ``investigate.py``, never by words in the person's message. A quick factual lookup in conversation stays a web search and is not recorded. Registers
its builders into ``tools.STANDARD_TOOL_BUILDERS`` when imported, beside the experiment tools.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional

from quest_ai_runner.the_world.science import investigate
from quest_ai_runner.the_world.tools.tools import (
    STANDARD_TOOL_BUILDERS, ToolContext, ToolResult, ToolSpec, _QUEST_ID_PARAM, _quest_id, quest_client_from_env, quest_credentials_present, data_of,
    write_failed)
from quest_ai_runner.the_world.tools.tools_status import ToolStatus

#: What a recorded investigation is for. ``none`` is a quick factual lookup answered in the reply: it is never recorded.
INFORMS = ("quest_decision", "experiment_question", "person_asked_for_research", "none")
ORIGINS = ("chat", "autopilot", "experiment")
SIZES = investigate.SIZES

INVESTIGATE_ARGS = {
    "type": "object",
    "required": ["question", "informs"],
    "properties": {
        "quest_id": _QUEST_ID_PARAM,
        "question": {"type": "string", "description": "What to find out, in one sentence."},
        "decision": {"type": "string", "description": "The decision on the quest the answer informs, in plain words."},
        "informs": {"type": "string", "enum": list(INFORMS),
                    "description": ("What the result is for: quest_decision (it informs a decision on this quest), experiment_question (it bears on a running "
                                    "experiment's research question), person_asked_for_research (the person asked for research), or none (a quick "
                                    "factual lookup answered in the reply: nothing is recorded).")},
        "size": {"type": "string", "enum": list(SIZES), "description": "quick unless the person asked for more. Never full unless they asked for a full investigation."},
        "person_asked_for_full_investigation": {"type": "boolean", "description": "True only when the person asked for a thorough, full investigation."},
        "research_question_id": {"type": "string", "description": "The experiment it is done inside, from find_quest_experiments."},
        "goal_id": {"type": "string", "description": "The goal it is done beside."},
        "asked_by": {"type": "string", "enum": ["person", "quest_ai"], "description": "quest_ai only when Quest AI began it on its own inside an autopilot pass or an experiment."},
        "origin": {"type": "string", "enum": list(ORIGINS)},
        "look_again": {"type": "boolean", "description": "True when the person asked to look again even though an earlier investigation is current."},
    },
}


def refused(text: str) -> ToolResult:
    return ToolResult(ok=False, status=ToolStatus.REFUSED, text=text)


def investigate_for_quest_spec(env: Mapping[str, str], client_factory=None, ask: Optional[Callable[[str], str]] = None,
                               carry_out: Optional[Callable[..., Mapping[str, Any]]] = None) -> ToolSpec:
    """Look into a question for a quest and record the investigation. ``ask`` (a fast-tier model call) and ``carry_out`` (the hub's review) are injectable."""
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        question = str(args.get("question") or "").strip()
        if not quest_id or not question:
            return refused("An investigation needs a quest and a question; nothing was looked into.")
        informs = str(args.get("informs") or "none")
        if informs not in INFORMS or informs == "none":
            return refused("Not recorded: a quick factual lookup is answered in the reply with a web search, and only a result that informs a decision on the quest, "
                           "an experiment's research question, or research the person asked for is investigated and recorded.")
        size = str(args.get("size") or "quick")
        size = size if size in SIZES else "quick"
        note = ""
        if size == "full" and not args.get("person_asked_for_full_investigation"):
            size, note = "standard", " It was run as a standard investigation, not a full one, because the person did not ask for a full one."
        client = factory()
        try:
            experiment = None
            research_question_id = str(args.get("research_question_id") or "").strip()
            if research_question_id:
                experiment = next((e for e in client.list_experiments(quest_id) if e.get("research_question_id") == research_question_id), None)
                if experiment is None:
                    return refused(f"Experiment {research_question_id} is not on this quest, so nothing was looked into.")
            solutions = client.investigation_solutions(quest_id)
        except Exception as e:  # QuestApiError / QuestNotConfigured: report, never raise
            return ToolResult(ok=False, text=f"Could not read the quest's strategies and experiments: {e}")
        decision = str(args.get("decision") or "").strip()
        placed = investigate.place(question, decision, [{"id": s["id"], "name": s.get("name")} for s in solutions], experiment, ask)
        if not placed.within_scope:
            return refused(f"Not looked into: {placed.scope_note} A running experiment's scope and what it holds fixed are not reopened before its reevaluation. "
                           f"Offer to set the thought aside for the reevaluation instead.")
        try:
            earlier = client.earlier_investigations(quest_id, placed.solution_ids, research_question_id)
        except Exception as e:
            return ToolResult(ok=False, text=f"Could not read the earlier investigations: {e}")
        answer = investigate.answering(question, decision, earlier, ask)
        extends = ""
        if answer is not None and answer.get("reuse") == investigate.FRESH and not args.get("look_again"):
            return ToolResult(ok=True, status=ToolStatus.NOOP, data=answer,
                              text=investigate.summary_text({**answer, "synthesis": {"known": answer.get("known"), "unknown": answer.get("unknown")}}, reused=True)
                              + f" Cite it as the earlier investigation {answer.get('investigation_id')} from {answer.get('done_on')}; nothing new was searched.")
        if answer is not None:
            extends = str(answer.get("investigation_id"))
        asked_by = "quest_ai" if args.get("asked_by") == "quest_ai" else "person"
        origin = args.get("origin") if args.get("origin") in ORIGINS else "chat"
        criteria = {"include_terms": [], "exclude_terms": []}
        try:
            hub_record = (carry_out or investigate.review)(question, decision, size, research_question_id=research_question_id, criteria=criteria,
                                                           solution_ids=placed.solution_ids)
        except Exception as e:  # the hub's seat may be pending a key: say so, record nothing
            return ToolResult(ok=False, status=ToolStatus.FAILED, text=f"The investigation could not be carried out, so nothing was recorded: {e}")
        body = investigate.quest_record(hub_record, solution_ids=placed.solution_ids, research_question_id=research_question_id,
                                        goal_id=str(args.get("goal_id") or ""), asked_by=asked_by, origin=origin, extends=extends)
        try:
            made = data_of(client.record_investigation(quest_id, question, **{k: v for k, v in body.items() if k != "question"}))
        except Exception as e:
            return write_failed(e, "Nothing was recorded")
        made = made or {}
        if not made.get("investigation_id"):
            return ToolResult(ok=False, status=ToolStatus.FAILED, text="Nothing was recorded: the investigation was refused.")
        extended = f" It extends the earlier investigation {extends}." if extends else ""
        return ToolResult(ok=True, data=made, text=investigate.summary_text(made) + f" Recorded as {made['investigation_id']}." + extended + note)

    return ToolSpec(
        name="investigate_for_quest",
        handler=handler,
        description=("Look into a question outside the person's own material as a small review (searches with dates, findings each graded for how sure it is, "
                     "what is known and not) and record it on the quest under the narrowest strategy it bears on. A current earlier investigation that already "
                     "answers the question is used and cited instead of searching again; an out of date or thin one is extended. Say in the reply what was "
                     "looked into, how sure the result is, and what it changed or proposes."),
        when_to_use=("The result informs a decision on the quest, bears on a running experiment's research question, or the person asked for research. "
                     "Size quick unless they asked for more."),
        when_not_to_use=("A quick factual lookup answered in conversation (use web search, set informs to none, nothing is recorded). Anything the quest's own "
                         "records already answer. A question outside a running experiment's scope or one that reopens what it holds fixed."),
        parameters=INVESTIGATE_ARGS,
        mutates=True,
        workflow_step=False,
        origin="standard",
        keywords=("investigate", "investigation", "research", "look into", "evidence", "sources", "review"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=600.0,
    )


def find_earlier_investigations_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """Read what was looked into on a quest, on a strategy and the strategies above it, or inside an experiment."""
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        if not quest_id:
            return ToolResult(ok=False, text="No quest was named, so there are no investigations to read.")
        solution_id = str(args.get("solution_id") or "").strip()
        research_question_id = str(args.get("research_question_id") or "").strip()
        try:
            client = factory()
            rows = (client.earlier_investigations(quest_id, [solution_id] if solution_id else [], research_question_id)
                    if solution_id or research_question_id else client.list_investigations(quest_id))
        except Exception as e:
            return ToolResult(ok=False, text=f"Could not read the investigations: {e}")
        if not rows:
            return ToolResult(ok=True, data=[], text=f"Nothing has been looked into yet on quest {quest_id} for that.")
        lines = [f"{investigate.reuse_line(r)}" + (f" | {r['reuse']}: " + {"fresh": "still current, use and cite it", "partial": "thin, extend it",
                                                                           "stale": "out of date, redo it starting from it"}[r["reuse"]] if r.get("reuse") else "")
                 for r in rows[:20]]
        return ToolResult(ok=True, data=rows, text="\n".join(lines))

    return ToolSpec(
        name="find_earlier_quest_investigations",
        handler=handler,
        description=("List what was already looked into on a quest. With a strategy id (or the quest's own id for the whole quest) it returns the earlier "
                     "investigations on that strategy and on every strategy above it, and on the experiment when one is given, each marked fresh (use and "
                     "cite it), thin (extend it) or out of date (redo it starting from it)."),
        when_to_use="Before looking anything up for a decision, and to answer what has been looked into already.",
        when_not_to_use="Reading the person's own notes or data.",
        parameters={"type": "object", "properties": {"quest_id": _QUEST_ID_PARAM, "solution_id": {"type": "string"}, "research_question_id": {"type": "string"}}},
        mutates=False,
        origin="standard",
        keywords=("investigation", "investigations", "researched", "looked into", "earlier", "evidence"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def mark_investigation_item_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """Say what one finding of an investigation changed, or why it changed nothing."""
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        investigation_id, item_id = str(args.get("investigation_id") or "").strip(), str(args.get("item_id") or "").strip()
        if not (quest_id and investigation_id and item_id) or not isinstance(args.get("applied"), bool):
            return refused("Needs the quest, the investigation, the finding and whether it was applied; nothing was recorded.")
        try:
            made = data_of(factory().mark_investigation_item(quest_id, investigation_id, item_id, args["applied"], what=args.get("what"), reason=args.get("reason"),
                                                              kind=args.get("kind"), record_id=args.get("record_id")))
        except Exception as e:
            return write_failed(e, "Nothing was recorded")
        return ToolResult(ok=True, data=made, text=f"Recorded that finding {item_id} of {investigation_id} " + ("changed: " + str(args.get("what")) if args["applied"]
                                                                                                          else "was not applied: " + str(args.get("reason"))) + ".")

    return ToolSpec(
        name="mark_quest_investigation_item",
        handler=handler,
        description=("Record what one finding of an investigation changed (applied, with what it changed) or why it changed nothing (not applied, with the "
                     "reason). It records the outcome only: a change to the person's records goes through the usual confirmation first, and a person's goal is "
                     "never renamed or re-described."),
        when_to_use="After the person confirmed a change a finding led to, or when a finding was weighed and set aside.",
        when_not_to_use="Making the change itself.",
        parameters={"type": "object", "required": ["investigation_id", "item_id", "applied"], "properties": {
            "quest_id": _QUEST_ID_PARAM, "investigation_id": {"type": "string"}, "item_id": {"type": "string"}, "applied": {"type": "boolean"},
            "what": {"type": "string", "description": "What it changed, in plain words (when applied)."},
            "reason": {"type": "string", "description": "Why it changed nothing (when not applied)."},
            "kind": {"type": "string", "enum": ["note", "goal", "strategy", "quest_field", "experiment", "decision", "other"]},
            "record_id": {"type": "string"}}},
        mutates=True,
        workflow_step=False,
        origin="standard",
        keywords=("investigation", "finding", "applied", "changed"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def with_credentials(build):
    return lambda env: build(env) if quest_credentials_present(env) else None


STANDARD_TOOL_BUILDERS.update({
    "investigate_for_quest": with_credentials(investigate_for_quest_spec),
    "find_earlier_quest_investigations": with_credentials(find_earlier_investigations_spec),
    "mark_quest_investigation_item": with_credentials(mark_investigation_item_spec),
})
