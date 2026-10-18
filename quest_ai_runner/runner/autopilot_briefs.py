"""The text of autopilot work: last-run summaries, next steps and the batch brief.

Split out of ``autopilot.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

from .quest_folder_sync import NextSteps
from ..core import prompt_budget
from .autopilot_defaults import (
    AUTOPILOT_BRIEF_TOKEN_BUDGET,
    AUTOPILOT_PASS_KIND,
    AUTOPILOT_WORK_KIND,
    LAST_RUN_CUT_MARKER,
    LAST_RUN_OUTPUT_MAX_CHARS,
    PROPOSAL_TEXT_PREFIX,
    _LADDER_SCOPE_LABELS,
    _MD_TITLE_FURNITURE_RE,
    log,
)
from .autopilot_periods import _parse_dt


def _summarize_previous(previous: Dict[str, Any]) -> str:
    """The "what happened last period" block, or a plain statement that nothing did.

    Saying so explicitly matters: an absent section reads as "no information", while "no recorded
    activity" is itself the signal that the previous period produced nothing and the plan may need
    re-sequencing rather than continuing as written.
    """
    period = previous.get("period") or "the previous period"
    lines = [f"What happened in the previous period ({period}):"]
    done_goals = [g for g in previous.get("goals") or [] if g.get("completed")]
    open_goals = [g for g in previous.get("goals") or [] if not g.get("completed")]
    tasks = previous.get("tasks") or []
    if done_goals:
        lines.append("  Goals completed: "
                     + "; ".join((g.get("name") or "?").strip() for g in done_goals))
    if open_goals:
        lines.append("  Goals left INCOMPLETE (carry them or re-sequence, do not silently drop "
                     "them): " + "; ".join((g.get("name") or "?").strip() for g in open_goals))
    dismissed_any = False
    for t in tasks:
        title = (t.get("title") or t.get("text") or "").strip().splitlines()
        label = (title[0] if title else "(untitled task)")[:90]
        outcome = str(t.get("result") or "").strip().replace("\n", " ")
        # A dismissal is the person saying this run was not worth their attention. It is the only
        # unprompted signal the feed produces, and it is cheap for them to give, so it is worth
        # more than its size: several dismissals in a row say the work itself is off, not that the
        # summary was long.
        dismissed = " [they cleared this from their feed]" if t.get("dismissed_at") else ""
        dismissed_any = dismissed_any or bool(dismissed)
        lines.append(f"  Task [{t.get('status')}] {label}{dismissed}"
                     + (f" -> {outcome[:280]}" if outcome else ""))
    if dismissed_any:
        lines.append("  A cleared run is feedback, not a failure and not a request: they saw it "
                     "and did not want it. Treat it as a signal about what to surface, and never "
                     "as work to redo, re-send or ask them about.")
    if not (done_goals or open_goals or tasks):
        lines.append("  No recorded activity. Treat the plan's schedule as untouched, and if that "
                     "is because work slipped, say so rather than repeating the same instruction.")
    return "\n".join(lines)


def select_last_run_output(tasks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The task whose result IS this quest's own last run, with no period window at all.

    ``_previous_period_summary`` answers "what finished in the previous calendar period", which is
    the wrong question for a quest whose scope label is coarser than a day: a week/month/quarter/
    year-scoped quest's own PREVIOUS period is last week/month/quarter/year, so a pass that ran on
    this quest YESTERDAY sits inside the CURRENT period and never appears there at all. An
    unscoped quest fares worse: ``_previous_period_summary`` returns ``None`` outright, so it gets
    no continuity whatsoever. This function answers a different, simpler question instead -- "what
    is the most recent thing this quest's own runner output actually said", full stop, independent
    of any period boundary -- which is answerable for every quest, scoped or not.

    Selection, in order:

      1. Only ``AUTOPILOT_PASS_KIND``/``AUTOPILOT_WORK_KIND`` rows with a non-empty stripped
         ``result`` are candidates at all. Anything else (an ordinary deep-run task, a row that has
         not finished yet) is not this quest's autopilot history.
      2. A goal proposal is never a candidate. Its ``text`` (not its ``result``) starts with
         ``PROPOSAL_TEXT_PREFIX``, and a proposal is a question waiting on the person, not a piece
         of produced work -- carrying it forward as "what the last run produced" would hand the
         next run its own unanswered question back as though it were an answer.
      3. Recency is the ONLY ordering, by ``worked_at`` or ``updated_at`` or ``created_at``
         (``_parse_dt``, same field precedence ``_previous_period_summary`` already uses, so the
         two never disagree about when a task happened). A candidate with no parseable timestamp is
         dropped rather than sorted arbitrarily.
      4. If the most recent candidate is a WORK row, and its ``parent_task_id`` names a PASS row
         that is ALSO present in ``tasks`` and ALSO carries a non-empty result, the PASS row is
         returned instead of the WORK row. This is not a tie-break, it is recognizing the SAME
         work under two names: quest-backend's ``autopilot_rollup.roll_up_onto_pass`` writes a
         finished work task's output back onto its parent pass row once it reaches a terminal
         status (see that module's docstring), specifically so the pass ends up holding the work
         itself rather than its own "Created N task(s)" bookkeeping. Once that rollup has run, the
         pass row is the more complete of the two -- a pass with several children carries ALL of
         their outputs, not just the one that happens to have finished most recently -- so it is
         preferred whenever it is available.

    Returns ``None`` when nothing qualifies (a fresh quest, or a quest whose only rows are
    proposals, empty, or unparseable).
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    dated: List[Tuple[datetime, Dict[str, Any]]] = []
    for t in tasks:
        task_id = str(t.get("id") or t.get("task_id") or "").strip()
        if task_id:
            by_id[task_id] = t
        if t.get("task_kind") not in (AUTOPILOT_PASS_KIND, AUTOPILOT_WORK_KIND):
            continue
        if str(t.get("text") or "").lstrip().startswith(PROPOSAL_TEXT_PREFIX):
            continue
        if not str(t.get("result") or "").strip():
            continue
        when = _parse_dt(t.get("worked_at") or t.get("updated_at") or t.get("created_at"))
        if when is None:
            continue
        dated.append((when, t))
    if not dated:
        return None
    dated.sort(key=lambda pair: pair[0])
    latest = dated[-1][1]
    if latest.get("task_kind") == AUTOPILOT_WORK_KIND:
        parent_id = str(latest.get("parent_task_id") or "").strip()
        parent = by_id.get(parent_id) if parent_id else None
        if (parent is not None and parent.get("task_kind") == AUTOPILOT_PASS_KIND
                and str(parent.get("result") or "").strip()):
            latest = parent
    return latest


def render_last_run_output(task: Dict[str, Any]) -> str:
    """Render ``task`` (from ``select_last_run_output``) as the batch's last-run-output block.

    The framing sentence says what this IS and what to do with it, on purpose: without it, a run
    reading a block of unlabeled prior output has no way to tell whether it is looking at
    instructions, at somebody else's work, or at its own quest's history, and the natural failure
    mode is to treat it as more work to do on top of rather than a foundation to build on or
    correct. So the sentence names it plainly (this is what the last run on this quest actually
    produced), gives the instruction that follows from that (build on it, do not restart from
    zero), and gives the escape hatch a continuity feature always needs (if it was wrong or has
    been overtaken, say so in THIS run's own result rather than silently contradicting it, since a
    silent contradiction reads to the person as the assistant not knowing what it said last time).

    The date and status ride in the same sentence because they change what the block below means:
    a run that ended ``done`` is a finished answer, while one that ended ``needs_you`` or
    ``failed`` stopped short, and "build on this" means something different for each -- extend a
    finished answer, but treat an unfinished one as an attempt rather than a settled conclusion.

    Truncated at ``LAST_RUN_OUTPUT_MAX_CHARS`` with a visible marker when cut, so a reader (human
    or model) never mistakes a truncated block for the complete output and never has to guess
    whether more existed.
    """
    when = _parse_dt(task.get("worked_at") or task.get("updated_at") or task.get("created_at"))
    date_str = when.strftime("%Y-%m-%d") if when else "an unknown date"
    status = str(task.get("status") or "unknown")
    status_note = ""
    if status == "needs_you":
        status_note = (" -- it stopped short, waiting on a person, so treat it as an attempt "
                       "rather than a settled answer")
    elif status == "incomplete":
        status_note = (" -- it ran but did not fully reach its goal, so treat it as an attempt "
                       "rather than a settled answer")
    elif status == "failed":
        status_note = (" -- it ended in failure, so treat it as an attempt rather than a settled "
                       "answer")
    # The RESULT only, never anything it quotes: a context-updates block inside an earlier output
    # is that run's material, and carried forward it would nest inside every later brief.
    result = prompt_budget.strip_blocks(str(task.get("result") or "").strip()).strip()
    if len(result) > LAST_RUN_OUTPUT_MAX_CHARS:
        result = (result[:LAST_RUN_OUTPUT_MAX_CHARS].rstrip()
                  + "\n\n" + LAST_RUN_CUT_MARKER)
    return (f"What the last run on this quest actually produced ({date_str}, ended {status}"
           f"{status_note}). Build on it rather than starting over: if it was wrong or has been "
           f"overtaken, say so plainly in this run's own result instead of silently contradicting "
           f"it.\n\n{result}")


def next_steps_from_pass(current_goals: List[Dict[str, Any]],
                         adopted_tasks: Optional[List[Dict[str, Any]]] = None, *,
                         scope_label: str = "", updated: str = "",
                         previous: Optional[Dict[str, Any]] = None,
                         reflection_note: str = "",
                         insights_note: str = "") -> NextSteps:
    """The pass's own conclusion about what comes next, as the canonical artifact.

    Deterministic and LLM-free, like ``propose_next_goal``: the pass has already read the quest's
    current scope, its goals at that scope and the recurring tasks it took over, so the artifact is
    a restatement of what it acted on, not a second opinion about it. Asking a model to re-derive
    it here would spend a call to produce a DIFFERENT answer from the one the pass just acted on,
    which is the exact drift this artifact exists to remove.

    ``current_goals`` is the quest's own goals at the scope it is working in (the ladder's finest
    rung), which is what "what is next for this quest" means to the person who wrote them. One line
    each, with the deadline where there is one, since "next" and "by when" are the same question.
    Then one line per adopted recurring task, then the previous period's unfinished goals as
    carry-over.

    ``reflection_note`` is one condensed line from the person's own latest reflection, and
    ``insights_note`` one from their newest unacted capture; both are carried into the artifact's
    ``note`` slot. They are context for the list, never steps: they explain WHY these are the next
    steps, and a reader who disagrees with the list can see what it was written against instead of
    guessing. An insight is explicitly NOT promoted to a step here — the person captured it, they
    did not commit to it, and quietly turning a note-to-self into a planned action is how an
    artifact stops being trustworthy.
    """
    steps: List[str] = []
    for goal in current_goals:
        name = (goal.get("name") or "(untitled goal)").strip()
        deadline = (goal.get("deadline") or "").strip()
        steps.append(f"{name}{f' (target {deadline})' if deadline else ''}")
    for task in adopted_tasks or []:
        label = str(task.get("title") or task.get("text") or "").strip().splitlines()
        if label:
            steps.append(f"{label[0][:120]} (recurring)")
    carrying = [(g.get("name") or "?").strip()
                for g in ((previous or {}).get("goals") or []) if not g.get("completed")]
    note = "\n".join(n for n in ((reflection_note or "").strip(),
                                 (insights_note or "").strip()) if n)
    return NextSteps(steps=steps, carrying_over=carrying, source="the autopilot pass",
                     scope=scope_label or "", updated=updated or "", note=note)


# Always told to a batch run, with or without previous-period rows to read it against.
#
# A recurring pass has no way to observe whether a person did the thing it asked for. Left to
# infer, it treats its own assignment as the event -- it asked yesterday, a day passed, so it
# moves on and asks for something else. The person then receives a new instruction every period
# while the first one is still untouched, which reads as the AI not noticing them at all. The
# fix is not more memory; it is being explicit that completion is a claim only the person can
# make, and that "re-sequence" means moving when work happens, never quietly substituting a
# fresh item for an untouched one.
_CONFIRMATION_RULE = (
    "WHAT COUNTS AS DONE. Only the person confirms their own work: a goal they completed, a note "
    "or reply they wrote, or an artifact they produced. None of these are confirmation -- that "
    "you assigned it, that an earlier run's plan listed it, that the period ended, or that you "
    "read the material yourself. You may never record their work as done on their behalf.\n"
    "While something you asked of them is unconfirmed, repeat THAT item rather than replacing it "
    "with a fresh one of the same kind, and say how long it has been outstanding. Re-sequencing "
    "means changing WHEN the remaining work happens; it never means swapping an untouched item "
    "for a different one, because someone who has not done the first thing is not helped by being "
    "handed a second. If the same item goes unconfirmed three times, stop reissuing it and make "
    "what to do about it the question you raise -- shrink it, swap it, or drop it."
)


_INSTRUCTIONS_PREAMBLE = (
    "Standing instructions for this quest, written by the person who owns it. They are the "
    "specification for this run: they say what to produce and how. Everything below is the "
    "material to apply them to. Follow them verbatim where they are specific."
)


# The same idea one level down, for the instructions a character carries on this quest. Two things
# are said explicitly rather than left to position. WHO it is addressed to, because this block sits
# directly under a quest-wide block that describes a different job, and "instructions for this
# character" is not actionable unless the text names the character. And WHICH governs, because two
# specifications in one brief with nothing ranking them is how a run quietly follows the more
# general one. ``{who}`` is filled with the persona's name when one is resolved.
_PERSONA_INSTRUCTIONS_PREAMBLE = (
    "Standing instructions {who} on this quest, written by the person who owns it. They describe "
    "the job this character does here, which is not the same job the quest-wide instructions "
    "describe. They are MORE SPECIFIC than those, so where the two disagree, follow these and say "
    "in your result which you followed and why."
)


# The framing for a slot the person has not written, one per level. Both say outright that the
# text is the built-in one rather than theirs, because the written preambles above claim authorship
# ("written by the person who owns it") and that claim is simply false of a default: a run told it
# about words nobody wrote would report back against a standard nobody set. The honest framing is
# also the useful one, since it tells the run this is the floor and that anything the person later
# writes replaces it outright.
#
# Everything else about the two blocks is unchanged, and deliberately so: a defaulted slot occupies
# the same position and carries the same authority as a written one, because it is the brief that
# actually governs the run.
_DEFAULT_INSTRUCTIONS_PREAMBLE = (
    "Standing instructions for this quest. Nobody has written a brief for this quest yet, so this "
    "is the built-in one, and it is the specification for this run: it says what to produce and "
    "how. Everything below is the material to apply it to. The moment the person writes "
    "instructions of their own for this quest, theirs replace this entirely."
)


# The persona-level default. It states the same precedence its written counterpart states, because
# the precedence is a property of the LEVEL and not of who wrote the text: this block still
# describes one character's own job, and still governs where it disagrees with the quest-wide
# block. ``{who}`` is filled with the persona's name when one is resolved.
_DEFAULT_PERSONA_INSTRUCTIONS_PREAMBLE = (
    "Standing instructions {who} on this quest. Nobody has written a brief for this character "
    "here yet, so this is the built-in one. It describes the job this character does here, which "
    "is not the same job the quest-wide instructions describe. It is MORE SPECIFIC than those, so "
    "where the two disagree, follow this and say in your result which you followed and why. The "
    "moment the person writes instructions for this character, theirs replace this entirely."
)


# The framing for the goal ladder (``current_goal_ladder``). Three things have to be said outright
# rather than left to position, because the block LOOKS like a work list and a run that reads it as
# one does exactly the wrong thing with it: that these goals are the person's and not this run's
# assignment, that goals the AI will never touch are on the list on purpose, and that the job is to
# make this run's output add up to them. Without the last sentence the ladder is just more text;
# with it, it is the check the run applies to its own result.
_GOAL_LADDER_PREAMBLE = (
    "THE PERSON'S CURRENT GOALS, at every horizon they plan in. This is the frame around this run, "
    "not a list of work for it. This run does not own these goals: do not try to finish them, do "
    "not report on them, and never record any of them as done. Goals the person is plainly doing "
    "themselves are included deliberately, because work that ignores them is work pulling against "
    "the plan rather than with it.\n"
    "Before you finish, check that what you produced actually serves these goals and adds up to "
    "the horizons above them. If it does not, say so plainly in your result instead of leaving the "
    "person to notice."
)


def _render_goal_ladder(ladder: List[Dict[str, Any]]) -> str:
    """The goal ladder as the text a run reads: the framing, then one short line per goal."""
    lines = [_GOAL_LADDER_PREAMBLE]
    for rung in ladder:
        scope = str(rung.get("scope") or "")
        label = _LADDER_SCOPE_LABELS.get(scope, scope or "This period")
        period = str(rung.get("period") or "").strip()
        lines.append(f"{label}{f' ({period})' if period else ''}:")
        for goal in rung.get("goals") or []:
            name = str(goal.get("name") or "").strip() or "(untitled goal)"
            deadline = str(goal.get("deadline") or "").strip()
            lines.append(f"  - {name}{f' (by {deadline})' if deadline else ''}")
        more = int(rung.get("more") or 0)
        if more > 0:
            # Said, never silently dropped: a person who plans twenty goals in a month must not be
            # shown eight of them as though that were the whole plan.
            lines.append(f"  +{more} more")
    return "\n".join(lines)


def compose_batch_text(quest_outcome: str,
                       persona: Optional[str] = None, *,
                       scope_label: Optional[str] = None,
                       adopted_tasks: Optional[List[Dict[str, Any]]] = None,
                       next_steps: Optional[str] = None,
                       previous: Optional[Dict[str, Any]] = None,
                       last_run: Optional[str] = None,
                       reflection: Optional[str] = None,
                       insights: Optional[str] = None,
                       context_updates: Optional[str] = None,
                       instructions: Optional[str] = None,
                       persona_instructions: Optional[str] = None,
                       default_quest_instructions: Optional[str] = None,
                       default_persona_instructions: Optional[str] = None,
                       goal_ladder: Optional[List[Dict[str, Any]]] = None,
                       experiments: Optional[str] = None,
                       asked: Optional[str] = None) -> str:
    """The batch task's text: what this run is asked to produce, the period and goals it serves,
    what the person themselves last said about the work, and what the previous period produced.

    The last two are what keep a recurring pass from starting cold every time. A daily pass that
    cannot see yesterday's goals and task results has no way to notice that the plan slipped, so it
    reissues the same instruction while the human falls further behind. Feeding the previous
    period's goal completion and task outcomes in makes continuity the default.

    ``reflection`` is the person's own latest daily/period reflection (``runner.reflections``), and
    ``insights`` the captures assigned to THIS quest that they have not yet acted on since the last
    pass (``runner.insights``). Those two are the only inputs here written BY them rather than derived
    from rows: everything else describes what the system recorded, while these say what the person
    made of it and what occurred to them in between, so they are what should break ties about what
    actually matters this run.

    The insights block carries only captures assigned to this quest when they were made (the
    backend's quest picker, or the person), each with its own category tags shown as their words.
    No code here matches a tag against a quest or goal name: that would be a fixed string rule,
    which this repository's hard rule #3 forbids.

    ``instructions``, when the quest carries any, is the block right after ``Scope:``: verbatim,
    never summarized/reflowed/rewritten (it is the one input here the person authored as a
    specification, not material to interpret). It comes before the material it governs, not after,
    because a specification that arrived after that material would read as commentary on work
    already planned. Absent, this emits nothing and the composed text is byte-identical to before
    this parameter existed.

    ``persona_instructions`` is the same thing one level down: the standing instructions THIS
    character carries on this quest, emitted immediately after the quest-wide block, equally
    verbatim. It exists because one roster can hold characters doing genuinely different jobs on
    the same quest -- the weekday worker carrying the ordinary work and the Saturday reviewer
    looking at the quest from outside -- and handing both the identical quest-wide brief describes
    neither of them. Its precedence over that brief is stated in its own framing sentence for the
    same reason the quest-wide one states its own. Absent, this likewise emits nothing and composes
    byte-identically to before the parameter existed.

    ``default_quest_instructions`` and ``default_persona_instructions`` are the FLOOR under those
    two, one each, and the layering rule lives here so there is exactly one place that decides it.
    THE TWO SLOTS DEFAULT INDEPENDENTLY: the quest-wide slot carries ``instructions`` when the
    person wrote them and its default otherwise, the persona slot carries ``persona_instructions``
    when the person wrote them and its default otherwise, and both slots are then emitted exactly
    as two written briefs are, quest-wide first and persona second. The two are not alternatives at
    all: one says how to work and how to deliver, the other says what to work on, so a quest that
    lost the second because its owner wrote the first would be a quest whose runs no longer know
    what they are for. A defaulted slot is framed as the built-in brief rather than as the
    person's, so a run is never told they wrote words they did not. Each default absent, that slot
    falls back to the written value alone, which is what keeps every direct caller's composition
    unchanged.

    ``goal_ladder`` (from ``current_goal_ladder``) is emitted after the brief: the run reads what
    it is asked to produce, then the picture that output has to fit into. It is the person's
    current goals at every horizon, and its framing says outright that they are context to serve
    rather than work to do. It is THE ONLY WAY A GOAL REACHES A RUN: no goal is ever composed as
    an assignment here. Absent or empty, this emits nothing and composes byte-identically to before
    the parameter existed.

    ``context_updates`` (from ``UpdateEngine.collect(...).as_prompt_block()``, see
    ``runner/context_updates.py``) is what has changed on channels OTHER than the two above since
    an assistant last looked at this quest -- a note the person added, a comment on a document this
    work owns, a file that changed. It carries its own ref-tagged manifest and its own receipt gate
    (``[U1]``, ``[U2]``, ...), so nothing here needs to render or explain it further; it is placed
    after the reflection and insights so the run reads its own instructions and the person's
    first-hand material first. A consumer that never builds an engine passes ``None``, and the
    composed text is byte-identical to before this parameter existed.

    ``last_run`` (from ``AutopilotPass._last_run_output``, already rendered by
    ``render_last_run_output``) is what this quest's own last run actually produced, with no
    period window: the previous-period block below only ever covers a task that finished inside
    the previous CALENDAR period, so a week/month/quarter/year-scoped quest cannot see its own run
    from yesterday there (yesterday sits in the CURRENT period), and an unscoped quest gets no
    previous-period view at all. This parameter is what makes "see the last run" true regardless
    of scope. Emitted immediately BEFORE the previous-period block, and that order is deliberate:
    the last run's actual output is more specific and more recent than the previous period's
    280-character status lines, so it reads first, with the coarser period-level view following as
    background. Absent, this emits nothing and the composed text is byte-identical to before this
    parameter existed.

    This is deliberately a PARAMETER here and not a registered source in
    ``runner/context_updates.py``, even though that module's own docstring names "add a parameter
    to the composer" as the anti-pattern it exists to kill. Two reasons, both specific to what this
    carries: (1) a registered source is watermarked -- delivered once, until ``mark_seen()`` moves
    the mark past it -- because it answers "what has arrived since an assistant last looked", and
    the previous run's output has to be in front of EVERY pass, not delivered once and then dropped
    the next time this quest runs (a quest passed twice in one day would otherwise start its
    second pass cold, which is the exact failure this feature exists to fix). (2) the registry
    models material that ARRIVED FROM A PERSON: ``QuestNotesSource``'s own docstring says outright
    that it filters to the person's own notes "so a quest an assistant writes a summary note to
    every day would [not] report its own output back to itself as news", and the receipt loop
    every registered source carries ("say in one line how you used [U1]") presumes the material
    came from somewhere outside the run reading it. ``last_run`` is the opposite: it is the
    assistant's OWN previous output, carried forward for continuity, not "new since last looked"
    and not something to acknowledge receipt of. The two are different questions -- what arrived,
    versus what continuity means -- so this stays its own parameter. See ``DEFAULT_ALWAYS`` in
    ``context_updates.py`` for the source-registry side of this same line.

    ``experiments`` (from ``quest_experiments.experiments_brief_section``) is the quest's
    experiments and what this run should do about them: carry the running ones forward, or, on a
    quest that has none and may plan, propose one. It is already compact and already carries its own
    instruction, so it rides in whole or not at all, at the standing-instructions tier: experiments
    are a core concern of every quest, so they outrank the goal frame, the plan of record and the
    history when the brief has to be cut. Absent or empty, this emits nothing and the composed text
    is byte-identical to before this parameter existed.

    ``asked`` (from ``quest_asks_brief.asks_brief_section``) is the What You Asked section: the
    quest's open asks with their keys, its standing rules, and what is already waiting on a person.
    The standing instructions tell a run to work these first, so it rides in whole or not at all at
    the same tier as the experiments. Absent or empty, the composed text is unchanged.

    ``persona``, when resolved, is named in the text AS WELL AS stamped structurally in
    ``assignee_rep_id`` at creation. The structured field is authoritative; the prose is kept
    because some consumers resolve the persona from the request text.
    """
    # Every part goes in as a prompt-budget Section with its priority (see
    # ``AUTOPILOT_BRIEF_TOKEN_BUDGET``): ``require`` adds the request tier, which is never dropped,
    # and ``add`` the optional tiers. Under budget the text is byte-identical to a plain join.
    sections: List[prompt_budget.Section] = []

    def require(text: str) -> None:
        sections.append(prompt_budget.Section("request", text, prompt_budget.PRIORITY_REQUEST,
                                              required=True))

    def add(name: str, text: str, priority: int, *, whole: bool = False) -> None:
        # ``whole``: never shortened, only dropped (a context-updates block cut mid-way would lose
        # its end marker and its receipt gate).
        sections.append(prompt_budget.Section(name, text, priority,
                                              floor_chars=len(text) if whole else None))

    if persona:
        require(f"Act as {persona}.")
    if quest_outcome:
        require(f"Quest outcome: {quest_outcome}")
    if scope_label:
        # Naming the period still matters, and for the reason it always did: a week's or a month's
        # worth of plan, read by a single run, reads as "do all of this today", which is both
        # discouraging and wrong. What changed is what the period holds. Its goals are context now
        # and never this run's assignment, so this line says which horizon the run is standing in
        # and stops there. Telling the run to advance the goals below would contradict the ladder
        # a few blocks down, which says in as many words that the run does not own them.
        require(f"Scope: this quest's {scope_label}. That is the period this run sits inside, "
                f"and it is longer than one session: it says which horizon everything here "
                f"belongs to, and it does not make the period's contents this run's workload. "
                f"What to produce is for the standing instructions to say. Where the period "
                f"itself has slipped or moved on, say so plainly.")
    # THE LAYERING RULE, and it is two rules that never look at each other. Each slot takes what
    # the person wrote for THAT slot, or its own built-in brief, and the framing is the only thing
    # that changes with the answer. A written brief at one level says nothing about the other
    # level, so it can never silence it.
    quest_brief = instructions or default_quest_instructions
    if quest_brief:
        preamble = _INSTRUCTIONS_PREAMBLE if instructions else _DEFAULT_INSTRUCTIONS_PREAMBLE
        require(preamble + "\n\n" + quest_brief)
    persona_brief = persona_instructions or default_persona_instructions
    if persona_brief:
        who = f"for {persona} specifically" if persona else "for this character specifically"
        template = (_PERSONA_INSTRUCTIONS_PREAMBLE if persona_instructions
                    else _DEFAULT_PERSONA_INSTRUCTIONS_PREAMBLE)
        require(template.format(who=who) + "\n\n" + persona_brief)
    if goal_ladder:
        # After the brief, deliberately: the run reads what it is asked to produce, then the goals
        # that output has to add up to. The reverse order reads as "here is a list of work, and
        # here is some more work", which is the misreading the framing above spends its words
        # preventing.
        add("goal frame", _render_goal_ladder(goal_ladder), prompt_budget.PRIORITY_GOALS)
    if asked:
        add("what you asked", asked, prompt_budget.PRIORITY_STANDING, whole=True)
    if experiments:
        add("experiments", experiments, prompt_budget.PRIORITY_STANDING, whole=True)
    if adopted_tasks:
        block = ["Recurring AI tasks for this period, adopted into this run. Carry out each one "
                 "as part of this run; the original occurrences are closed and will NOT run "
                 "separately, so anything you skip here simply does not happen:"]
        for t in adopted_tasks:
            text = prompt_budget.strip_blocks(str(t.get("text") or "").strip())
            block.append(f"\n--- adopted task {t.get('id') or t.get('task_id')} ---\n{text}")
        require("\n".join(block))
    if next_steps:
        # The quest folder's canonical next-steps artifact, which an attended session may have
        # refreshed more recently than any pass. Naming it as the standing answer is the point: two
        # sources for "what is next" is how a background run and the person working the quest end up
        # pulling in different directions without either noticing.
        add("plan of record",
            "The quest folder's standing next-steps artifact (QUEST_SYNC.md), which an "
            "attended session may have refreshed since the last pass. Treat it as the "
            "current plan of record, and if the work has moved past it, say so:\n"
            + next_steps, prompt_budget.PRIORITY_PLAN)
    if reflection:
        # Placed after the goals and the plan of record, and before the previous-period rows,
        # because it is the lens to read them through rather than another item on the list. It is
        # quoted as-is: paraphrasing a person's own words back at a model turns the one
        # first-hand input in this brief into a second-hand one.
        add("reflection",
            reflection + "\n\nLet that steer which of the above matters most in this run, "
            "and what tone to take. If it contradicts the plan above, say so plainly in "
            "your result rather than quietly following one or the other.",
            prompt_budget.PRIORITY_UPDATES)
    if insights:
        # Alongside the reflection, for the same reason and with the same framing: it is the
        # person's own material, and it is here to be judged rather than obeyed. The block already
        # carries the "decide which of these apply" instruction, since that judgment belongs to the
        # reader and never to a tag match in this code.
        add("insights", insights, prompt_budget.PRIORITY_UPDATES)
    if context_updates:
        add("context updates", context_updates, prompt_budget.PRIORITY_UPDATES, whole=True)
    if last_run:
        # BEFORE the previous-period block, not after: this is one specific run's actual output,
        # already rendered and already more recent than anything the coarser period-level view
        # below can offer, so it is the more useful thing to read first. See the docstring
        # paragraph on ``last_run`` for why this exists as its own channel at all.
        add("last run", last_run, prompt_budget.PRIORITY_HISTORY)
    if previous:
        add("previous period", _summarize_previous(previous), prompt_budget.PRIORITY_HISTORY)
    require(_CONFIRMATION_RULE)
    fit = prompt_budget.fit_sections(sections, AUTOPILOT_BRIEF_TOKEN_BUDGET)
    if fit.cuts:
        log.info("autopilot brief fitted to its budget: %s", fit.summary())
    return "\n\n".join(sec.text for sec in fit.sections if sec.text)


def _batch_title(adopted_tasks: Optional[List[Dict[str, Any]]] = None,
                 instructions: Optional[str] = None,
                 persona_instructions: Optional[str] = None) -> str:
    """A short label for the task list: what this batch is ABOUT.

    Without one the server derives a title from the first line of the text, which is the "Act as
    ..." persona line, so every autopilot task in the list is titled after its persona instead of
    its work. An adoption batch is titled after the task it took over; everything else is titled
    after the brief it is working to.

    A brief's title is its first non-empty line, stripped of leading Markdown furniture ("#", "-",
    "*", ">") and capped at 80 characters. That title is also the SUBJECT of the mail a finished run
    sends, which is why it matters here and not just cosmetically.

    ONLY WRITTEN BRIEFS MAY TITLE A BATCH, and that is the whole reason the defaults are not in
    this chain. A default is the same text on every quest that has not written one, so titling from
    it would name every task on every such quest "Ground every claim about where things stand...",
    forever, in the list and in the person's inbox. A title has to say what makes THIS run
    different, and a built-in brief says nothing of the kind. So the run composes with the
    effective text and is titled from the written text alone.

    The order is the same precedence the brief itself has. ``persona_instructions`` first, because
    a batch that exists because a character has their own standing job on this quest is about THAT
    job, and titling it after the quest's general brief would name work this run is not doing. Then
    the quest-wide ``instructions``. Then the plain "Autopilot run", which is what a fully defaulted
    quest gets: honest, and short enough that a person reading their list sees the quest's name
    doing the work instead.
    """
    if adopted_tasks:
        first = str((adopted_tasks[0].get("title") or adopted_tasks[0].get("text") or "")).strip()
        lines = first.splitlines()
        if lines and lines[0].strip():
            return lines[0].strip()[:120]
    for source in (persona_instructions, instructions):
        if not source:
            continue
        for line in source.splitlines():
            stripped = _MD_TITLE_FURNITURE_RE.sub("", line).strip()
            if stripped:
                return stripped[:80]
    return "Autopilot run"


def propose_next_goal(quest: Dict[str, Any]) -> Tuple[str, str]:
    """A deterministic (LLM-free, so this stays fast/offline-testable) proposed next-goal (title,
    description) in service of the quest's stated outcome, used when ``planning=="plan_and_work"``
    and the pass produced no work batch at all."""
    outcome = (quest.get("outcome") or "this quest's outcome").strip()
    title = f"Next step toward: {outcome}"
    description = (
        f'Propose and take the next concrete step toward "{outcome}". This pass produced no work '
        "on the quest, and the quest allows autopilot to plan as well as work. Define a specific, "
        "checkable outcome for this goal before starting on it."
    )
    return title, description
