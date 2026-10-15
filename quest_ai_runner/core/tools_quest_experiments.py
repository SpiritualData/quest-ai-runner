"""The Experiments section as registry tools: find, create and change a quest's experiments.

Split out of ``tools`` by role (that module passed its size limit); the names are re-exported
from ``tools`` so every existing import keeps working. Registers its three builders into
``tools.STANDARD_TOOL_BUILDERS`` when imported.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Sequence

from .tools import (STANDARD_TOOL_BUILDERS, TOKEN_RE, ToolContext, ToolResult, ToolSpec, _ASKED_PARAM,
                    _NEVER_CODE, _QUEST_ID_PARAM, _quest_id, quest_client_from_env,
                    quest_credentials_present)

# ---------------------------------------------------------------------------------------------
# Experiments: the quest's Experiments section (research questions), read / search / create / edit
# ---------------------------------------------------------------------------------------------
#
# An experiment is a question the person is testing in their own life, with the goals and habits
# that gather its data (quest-backend ``research_questions``). It is NOT a quest field, so these
# tools do not go through the field-update gate; they use the Experiments section's own routes,
# and so refuse to create or change one the person did not ask for. Searching by text is done here
# over the listing, so no second endpoint or hand-written database query is ever needed.

EXPERIMENT_STATUSES = ("open", "experimenting", "concluded")


_EXPERIMENT_FILLER = frozenset({
    "the", "and", "for", "that", "this", "with", "from", "about", "does", "did", "how", "what",
    "which", "when", "why", "can", "are", "was", "were", "has", "have", "into", "than", "then",
    "them", "they", "you", "your", "his", "her", "its", "our", "any", "all", "not", "but", "out",
    "off", "too", "find", "show", "search", "look", "get", "list", "experiment", "experiments",
    "question", "questions", "quest", "mine", "one", "ones", "please", "tell", "help", "helps"})


def _experiment_stem(word: str) -> str:
    for suffix in ("ing", "es", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _experiment_words(text: Any) -> List[str]:
    """Words of 3+ letters minus filler, lightly stemmed so "sleeping" meets "sleep"."""
    return [_experiment_stem(w) for w in TOKEN_RE.findall(str(text or "").lower().replace("_", " "))
            if len(w) > 2 and w not in _EXPERIMENT_FILLER]


def search_experiments(rows: Sequence[Mapping[str, Any]], query: str,
                       limit: int = 5) -> List[Mapping[str, Any]]:
    """Rows matching ``query``, best first: distinct query words found in the question, context,
    hypothesis, purpose, sub-questions, assumptions and conclusion, with a bonus for words in the question itself. The quest's default
    question is searched by its question text only, since the listing fills its context with the
    whole quest. A query with no usable word matches nothing (listing is the caller's job)."""
    wanted = set(_experiment_words(query))
    if not wanted:
        return []
    scored = []
    for row in rows:
        keys = ("question",) if row.get("kind") == "default" else (
            "question", "context", "hypothesis", "conclusion")
        body = " ".join(str(row.get(k) or "") for k in keys)
        if row.get("kind") != "default":
            body += " " + " ".join(
                [str(row.get("purpose") or "")]
                + [f"{sq.get('question') or ''} {sq.get('answer_method') or ''}"
                   for sq in row.get("sub_questions") or [] if isinstance(sq, Mapping)]
                + [str(a) for a in row.get("assumptions") or []])
        hits = wanted & set(_experiment_words(body))
        if hits:
            scored.append((len(hits) + 0.5 * len(wanted & set(_experiment_words(row.get("question")))),
                           row))
    scored.sort(key=lambda pair: -pair[0])
    return [row for _, row in scored[:limit]]


_EXPERIMENT_PURPOSE_PARAM = {
    "type": "string",
    "description": "Why they run it: how it ties to the quest. Only if they said."}
_EXPERIMENT_SUB_QUESTIONS_PARAM = {
    "type": "array",
    "description": ("Smaller questions beside the main one, only if they gave any. answer_method "
                    "(how it will be answered, or what analysis it needs) only if they said."),
    "items": {"type": "object", "required": ["question"],
              "properties": {"question": {"type": "string"},
                             "answer_method": {"type": "string"}}}}
_EXPERIMENT_ASSUMPTIONS_PARAM = {
    "type": "array", "items": {"type": "string"},
    "description": ("Assumptions that ground the experiment or state its constraints, one per "
                    "item, in their words. Only those they stated.")}


def _experiment_line(row: Mapping[str, Any]) -> str:
    bits = [f"[{row.get('research_question_id')}] {row.get('question')}",
            f"status {row.get('status')}"]
    if row.get("kind") == "default":
        bits.append("the quest's own question")
    if row.get("hypothesis"):
        bits.append(f"expects: {str(row['hypothesis'])[:160]}")
    if row.get("purpose"):
        bits.append(f"purpose: {str(row['purpose'])[:160]}")
    subs = [sq for sq in row.get("sub_questions") or [] if isinstance(sq, Mapping)]
    if subs:
        bits.append("sub-questions: " + " | ".join(
            str(sq.get("question") or "")[:120]
            + (f" (how: {str(sq['answer_method'])[:80]})" if sq.get("answer_method") else "")
            for sq in subs))
    if row.get("assumptions"):
        bits.append("assumptions: " + " | ".join(str(a)[:120] for a in row["assumptions"]))
    if row.get("start_date") or row.get("end_date"):
        bits.append(f"{row.get('start_date') or '?'} to {row.get('end_date') or '?'}")
    if row.get("conclusion"):
        bits.append(f"concluded: {str(row['conclusion'])[:160]}")
    return "; ".join(bits)


def find_quest_experiments_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """Read a quest's experiments, or search them by text, through the Experiments section's API."""
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        if not quest_id:
            return ToolResult(ok=False, text="No quest was named, so there are no experiments to read.")
        try:
            rows = factory().list_experiments(quest_id)
        except Exception as e:  # QuestApiError / QuestNotConfigured: report, never raise
            return ToolResult(ok=False, text=f"Could not read the experiments: {e}")
        query = str(args.get("query") or "").strip()
        if query:
            rows = search_experiments(rows, query, int(args.get("limit") or 5))
            if not rows:
                return ToolResult(ok=True, data=[], text=f"No experiment on quest {quest_id} "
                                                         f"matches {query!r}.")
        else:
            rows = rows[:int(args.get("limit") or 20)]
        if not rows:
            return ToolResult(ok=True, data=[], text=f"Quest {quest_id} has no experiments yet.")
        return ToolResult(ok=True, data=rows,
                          text="\n".join(_experiment_line(r) for r in rows))

    return ToolSpec(
        name="find_quest_experiments",
        handler=handler,
        description=("List a quest's experiments (the Experiments section: questions the person "
                     "is testing, with status, expectation, dates and conclusion), or search "
                     "them by words. Each line starts with the experiment id to use for an edit."),
        when_to_use=("Any question about the quest's experiments: what is running, find the "
                     "sleep experiment, what did it conclude, and before editing one (to get "
                     "its id) or creating one (to avoid a duplicate)."),
        when_not_to_use=("Reading the data an experiment collects (that is the tracking habit or "
                         "collection). Quest goals or habits."),
        parameters={
            "type": "object",
            "properties": {
                "quest_id": {"type": "string",
                             "description": "Quest to read (defaults to this chat's quest)."},
                "query": {"type": "string",
                          "description": "Words to search for; omit to list every experiment."},
                "limit": {"type": "integer", "description": "Most rows to return."},
            },
        },
        mutates=False,
        origin="standard",
        keywords=("experiment", "experiments", "research", "question", "hypothesis", "test",
                  "trial", "find", "search", "list", "show"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def create_quest_experiment_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """Create one experiment through ``POST /api/quests/{id}/research-questions``."""
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        question = str(args.get("question") or "").strip()
        if not quest_id or not question:
            return ToolResult(ok=False, text="An experiment needs a quest and a question; "
                                             "nothing was created.")
        if not args.get("user_asked_for_this_field"):
            return ToolResult(ok=False, text=(f"Not created: the user did not ask for the "
                                              f"experiment {question!r}. Suggest it in your "
                                              f"reply instead."))
        fields = {k: args[k] for k in ("context", "hypothesis", "purpose", "sub_questions",
                                       "assumptions", "linked_goal_ids",
                                       "linked_collection_ids", "start_date", "end_date")
                  if args.get(k) not in (None, "", [])}
        try:
            made = factory().create_experiment(quest_id, question, **fields)
        except Exception as e:  # QuestApiError / QuestNotConfigured: report, never raise
            return ToolResult(ok=False, text=f"Nothing was created: {e}")
        if not made:
            return ToolResult(ok=False, text=f"Nothing was created: the experiment {question!r} "
                                             f"was refused.")
        return ToolResult(ok=True, data=made,
                          text=(f"Created the experiment {question!r} on quest {quest_id} "
                                f"(id {made.get('research_question_id')}, status "
                                f"{made.get('status')})."))

    return ToolSpec(
        name="create_quest_experiment",
        handler=handler,
        description=("Create one experiment on a quest: a question the person wants to answer "
                     "by testing it in their own life. Use their words for the question; add a "
                     "hypothesis only if they stated what they expect, a purpose, sub-questions "
                     "(with an answer method) and assumptions only if they gave them, and link goals or "
                     "habits only by ids you read (never invent one). Check "
                     "find_quest_experiments first so you do not duplicate one."),
        when_to_use=("The user asked to start, set up or add an experiment on their quest. "
                     + _NEVER_CODE),
        when_not_to_use=("A goal, habit or task. An experiment the user did not ask for: "
                         "suggest it instead. Changing an existing experiment (use "
                         "update_quest_experiment)."),
        parameters={
            "type": "object",
            "required": ["question"],
            "properties": {
                "quest_id": _QUEST_ID_PARAM,
                "question": {"type": "string",
                             "description": "What they want to find out, in their words (max 500 chars)."},
                "context": {"type": "string", "description": "Why they ask; what bears on it."},
                "hypothesis": {"type": "string", "description": "What they expect, if they said."},
                "purpose": _EXPERIMENT_PURPOSE_PARAM,
                "sub_questions": _EXPERIMENT_SUB_QUESTIONS_PARAM,
                "assumptions": _EXPERIMENT_ASSUMPTIONS_PARAM,
                "linked_goal_ids": {"type": "array", "items": {"type": "string"},
                                    "description": "Goal ids that carry it out."},
                "linked_collection_ids": {"type": "array", "items": {"type": "string"},
                                          "description": "Habit / collection ids that collect its data."},
                "start_date": {"type": "string", "description": "YYYY-MM-DD, if given."},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, if given."},
                "user_asked_for_this_field": _ASKED_PARAM,
            },
        },
        mutates=True,
        workflow_step=False,  # needs the user's own say-so (user_asked_for_this_field); a scheduled step cannot assert it
        origin="standard",
        keywords=("experiment", "experiments", "research", "question", "hypothesis", "test",
                  "trial", "start", "set up", "create", "add", "new"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def update_quest_experiment_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """Change an experiment's details through ``PATCH /api/quests/{id}/research-questions/{rid}``."""
    factory = client_factory or (lambda: quest_client_from_env(env))
    editable = ("question", "context", "hypothesis", "purpose", "sub_questions", "assumptions", "status", "conclusion", "linked_goal_ids",
                "linked_collection_ids", "start_date", "end_date")

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        rid = str(args.get("research_question_id") or "").strip()
        if not quest_id or not rid:
            return ToolResult(ok=False, text="Editing an experiment needs a quest and the "
                                             "experiment's id (find_quest_experiments); nothing "
                                             "was changed.")
        if not args.get("user_asked_for_this_field"):
            return ToolResult(ok=False, text=(f"Not changed: the user did not ask to change "
                                              f"experiment {rid}. Suggest it in your reply "
                                              f"instead."))
        changes = {k: args[k] for k in editable if k in args and args[k] is not None}
        if not changes:
            return ToolResult(ok=False, text="Nothing to change: give at least one field.")
        if changes.get("status") and changes["status"] not in EXPERIMENT_STATUSES:
            return ToolResult(ok=False, text=f"status must be one of {list(EXPERIMENT_STATUSES)}.")
        try:
            updated = factory().update_experiment(quest_id, rid, **changes)
        except Exception as e:  # QuestApiError / QuestNotConfigured: report, never raise
            return ToolResult(ok=False, text=f"Nothing was changed: {e}")
        if not updated:
            return ToolResult(ok=False, text=f"Nothing was changed on experiment {rid}.")
        return ToolResult(ok=True, data=updated,
                          text=(f"Updated experiment {rid} on quest {quest_id}: "
                                f"{', '.join(sorted(changes))} (status now {updated.get('status')})."))

    return ToolSpec(
        name="update_quest_experiment",
        handler=handler,
        description=("Change details of an existing experiment: its question, context, "
                     "hypothesis, purpose, sub-questions, assumptions, status (open, experimenting, concluded), conclusion, linked "
                     "goal or habit ids, start or end date (an empty string clears a date). Send "
                     "ONLY the fields the user asked to change; the others stay as they are. "
                     "Get the id from find_quest_experiments. The quest's own default question keeps "
                     "its question text and context (they belong to the quest, so edit the "
                     "quest's fields for those); its status, hypothesis, conclusion, links "
                     "and dates can change here."),
        when_to_use=("The user asked to edit, rename, conclude, reopen, re-date or relink an "
                     "experiment. " + _NEVER_CODE),
        when_not_to_use=("Creating an experiment (use create_quest_experiment). Logging data for "
                         "it (write to the tracking habit or collection). A change the user did "
                         "not ask for: suggest it instead."),
        parameters={
            "type": "object",
            "required": ["research_question_id"],
            "properties": {
                "quest_id": _QUEST_ID_PARAM,
                "research_question_id": {"type": "string",
                                         "description": "The experiment's id, from find_quest_experiments."},
                "question": {"type": "string"},
                "context": {"type": "string"},
                "hypothesis": {"type": "string"},
                "purpose": _EXPERIMENT_PURPOSE_PARAM,
                "sub_questions": {**_EXPERIMENT_SUB_QUESTIONS_PARAM,
                                  "description": "The FULL new list of sub-questions."},
                "assumptions": {**_EXPERIMENT_ASSUMPTIONS_PARAM,
                                "description": "The FULL new list of assumptions."},
                "status": {"type": "string", "enum": list(EXPERIMENT_STATUSES)},
                "conclusion": {"type": "string"},
                "linked_goal_ids": {"type": "array", "items": {"type": "string"},
                                    "description": "The FULL new list of linked goal ids."},
                "linked_collection_ids": {"type": "array", "items": {"type": "string"},
                                          "description": "The FULL new list of linked habit / collection ids."},
                "start_date": {"type": "string", "description": "YYYY-MM-DD, or '' to clear."},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, or '' to clear."},
                "user_asked_for_this_field": _ASKED_PARAM,
            },
        },
        mutates=True,
        workflow_step=False,  # needs the user's own say-so (user_asked_for_this_field); a scheduled step cannot assert it
        origin="standard",
        keywords=("experiment", "experiments", "research", "question", "hypothesis", "conclude",
                  "conclusion", "edit", "change", "update", "rename", "reopen", "status"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def with_credentials(build):
    return lambda env: build(env) if quest_credentials_present(env) else None


STANDARD_TOOL_BUILDERS.update({
    "find_quest_experiments": with_credentials(find_quest_experiments_spec),
    "create_quest_experiment": with_credentials(create_quest_experiment_spec),
    "update_quest_experiment": with_credentials(update_quest_experiment_spec),
})
