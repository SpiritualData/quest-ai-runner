"""Autopilot constants and the bundled default instructions.

Split out of ``autopilot.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict

from ..core import prompt_budget


log = logging.getLogger("quest-ai-runner.autopilot")


# Team-wide daily cap on autopilot-created tasks (batches + goal proposals both count as one
# unit each). Overridable via ``RunnerConfig.autopilot_daily_budget``.
DEFAULT_TEAM_DAILY_BUDGET = 3


# --- the two PERSISTENT task_kind markers (see QuestClient.create_task) ---------------------
# ``task_kind`` is written once at create and never overwritten by the claim path (unlike
# ``handler``, which every claim stamps with the claiming worker's own label). Two DISTINCT values,
# and the distinction is load-bearing:
#   * PASS_KIND     -- the recurring "autopilot pass" task itself. The executor routes THIS to
#                      AutopilotPass instead of a deep run.
#   * WORK_KIND     -- the work batches / goal proposals the pass CREATES. They must NOT carry
#                      PASS_KIND, or the executor would route each created task back into another
#                      autopilot pass (an infinite self-spawning loop). They are ordinary tasks
#                      that happen to be autopilot-authored, and this marker is what makes them
#                      countable for the daily budget and the per-quest backpressure gate.
AUTOPILOT_PASS_KIND = "autopilot"


AUTOPILOT_WORK_KIND = "autopilot_work"


# The ``source`` stamped on autopilot-created tasks. The Quest API validates source against a
# CLOSED enum and 400s anything else (create_task raises, by design). The enum has since gained
# "autopilot", but "chat" is kept here because it is accepted by BOTH the current and older
# backends, and the authoritative autopilot-authored marker is ``task_kind``
# (AUTOPILOT_WORK_KIND above) rather than source. Deployments pinned to a current backend can
# switch this to AUTOPILOT_SOURCE below for attribution; the gates below count either.
AUTOPILOT_TASK_SOURCE = "chat"


# The dedicated source value on current backends. Rows stamped this way still count toward the
# budget and backpressure gates below, so the two can coexist during a rollout.
AUTOPILOT_LEGACY_SOURCE = "autopilot"


# Cadence name -> minimum days between passes for a given quest. An unrecognized cadence string
# falls back to "weekly" rather than erroring, so a bad/unknown value never wedges a quest either
# always-due or never-due.
_CADENCE_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}


# A previous autopilot task in any of these states is still "open" -- its quest is under
# backpressure and gets no more autonomous work piled on until it resolves.
OPEN_TASK_STATUSES = {"queued", "in_progress", "needs_you", "suggested"}


# Every status a task can hold (quest-backend ``TASK_STATUSES``). The daily budget counts a pass's
# output whatever state it has reached, so the count reads each one.
TASK_STATUSES_FOR_COUNT = ("cancelled", "done", "failed", "incomplete", "in_progress",
                           "needs_you", "queued", "suggested")


# Every proposed-goal task's text starts with this, which is how a later pass recognizes the
# proposal it already made and does not make it again (see ``_open_proposal``).
#
# It is also a CROSS-REPO wire contract: quest-backend re-declares the identical literal in
# app/business/quests/autopilot_rollup.py to tell a proposal from a piece of work when it renders
# the pass rollup. Rewording it here alone breaks nothing loudly -- the reader just stops
# recognizing proposals and files them under "Waiting for your approval before it can run" again.
# That backend's tests/business/test_autopilot_runner_contract.py asserts equality against THIS
# constant, so change both sides together and it will say so.
PROPOSAL_TEXT_PREFIX = "Proposed goal:"


# The roster id standing for the BUILT-IN default assistant, rather than for any real AI rep.
#
# A quest with an EMPTY roster has always produced one plain-assistant batch per due pass, which is
# the right behaviour for an unconfigured quest but leaves the person nothing to point at: days live
# on a roster entry, so "run this quest on Mondays and Thursdays" was unsayable until you had reps
# to pick from, and a personal quest never has any. An entry carrying this id is that same fallback
# written down: it goes on duty by the ordinary day rule, it can carry its own standing
# instructions, and every place that would otherwise hand the id to the consumer as a character
# resolves it back to the fallback (``_batch_persona``). Nothing is ever ASSIGNED to a rep by this
# name, because no such rep exists.
#
# quest-frontend declares the same literal as DEFAULT_PERSONA_REP_ID in
# src/components/quest/AutopilotPanel.tsx; the two are one contract, so change them together.
DEFAULT_ASSISTANT_REP_ID = "default_assistant"


# Time-scope granularities recognized in a quest's ``list_quest_goals`` period grouping, checked
# FINEST first (day) so "today" wins over a same-quarter month/quarter/year group that also
# happens to be current.
_SCOPE_ORDER = ("day", "week", "month", "quarter", "year")


# Task statuses that count as "this finished" when summarizing the previous period. ``needs_you``
# is included on purpose: a task waiting on the human is one of the most useful things the next
# pass can know, since it usually explains why the period produced nothing else.
_FINISHED_TASK_STATUSES = {"done", "failed", "incomplete", "needs_you"}


# Cap on how many previous-period tasks are described in a batch's text, newest kept. A busy quest
# should not push its actual instructions out of the model's attention with old status lines.
_MAX_PREVIOUS_TASKS = 8


# How much of the previous run's own output rides into the next run's brief. Far larger than the
# 280-character status line _summarize_previous gives a previous-period task, because this is not
# a status line: it is the work itself, and a run that can only see a headline of what it produced
# yesterday will redo it or contradict it.
LAST_RUN_OUTPUT_MAX_CHARS = 2000


LAST_RUN_CUT_MARKER = "[... cut here at this brief's last-run budget; this is not all of it]"


# The token budget for one composed batch brief, spent by priority through the same mechanism as
# the deep prompt it becomes part of (``core.prompt_budget``): the request (persona, outcome,
# scope, standing instructions, adopted tasks, the done rule) is kept; then the person's own fresh
# material and the context updates; then the goal frame; then the plan of record; and the last run
# and previous period are cut first. Half the default deep prompt budget, so the brief always
# leaves room for the context the deep run selects beside it.
AUTOPILOT_BRIEF_TOKEN_BUDGET = prompt_budget.DEFAULT_DEEP_PROMPT_TOKEN_BUDGET // 2


# A quest's standing ``autopilot.instructions``, in characters. Mirrors the backend's own
# ``AutopilotSettings.instructions`` cap (``max_length=8000``) so a value written before either
# cap existed, or written through some other client, still gets truncated defensively here rather
# than riding an oversized block into every prompt this quest ever gets.
MAX_INSTRUCTIONS_CHARS = 8000


# The standing briefs a batch works to where the person has written none of their own. There are
# TWO, and they default INDEPENDENTLY, because they answer two different questions: the quest-wide
# brief says HOW to work and how to deliver and applies to every character on the quest, while the
# persona brief says WHAT to work on and is one character's own job here. A quest whose owner wrote
# only one of them keeps their text for that slot and gets the built-in one for the other, rather
# than losing the built-in floor for both the moment they write a line anywhere.
#
# The authoritative copies are the backend's: a current quest payload carries them read-only as
# ``autopilot.default_quest_instructions`` and ``autopilot.default_persona_instructions``
# (server-derived, never client-settable), and THOSE values always win here. That is the point of
# serving them: improving the text on the server improves every quest that never wrote its own
# brief, immediately, with no client release and no per-quest migration.
#
# These bundled copies exist for the other case. Against a backend that predates the fields -- or
# any client that simply does not serve them -- a character on duty would otherwise have no
# specification at all and the pass would go silent, which is the exact failure these defaults were
# introduced to remove. So the runner degrades to a real brief rather than to nothing. They are a
# fallback and never a second source of truth: if the two ever disagree, the server's text is the
# one that ran.
# The texts below are VERBATIM copies of the server's, word for word, and must stay that way. They
# were written and confirmed by a person, so a paraphrase here is not a smaller version of the same
# brief: an earlier draft of this constant tightened the "doable without thinking" paragraph and
# lost its concrete list ("the link to open, the command to paste"), which is the entire instruction
# that paragraph exists to give. Two texts that differ are
# two briefs, and the one that runs against an older backend would be the weaker one. The backend's
# own runner-contract test asserts both pairs equal, so a reword here fails there. Copy them, never
# re-word them.

# HOW to work and how to deliver. The default for the QUEST-WIDE ``instructions`` slot, so it
# applies to every character the quest puts on duty.
BUNDLED_DEFAULT_QUEST_INSTRUCTIONS = (
    "Everything here works toward this quest's measurable outcomes. Name the outcome each piece of "
    "work moves and where it stands against its own number. Where the quest lists none, work toward "
    "the outcome as the person wrote it (their vision statement for this quest) and its acceptance "
    "criteria, and say which part you move. Work that moves neither does not belong in this run.\n\n"
    "Work in this order. First, the open asks in the What You Asked section of your brief: they are "
    "what the person asked for on this quest. Then the goals of the periods running now, shortest "
    "horizon first (today, this week, this month, the quarter, the year). Then anything you thought "
    "of yourself. Say which ask or goal each piece serves. Goals stay the person's, so never mark one "
    "done. Use update_ask to sort an ask that is not sorted yet (a request, a standing rule, "
    "background, or a question) and to record one you finish, so the person can accept it.\n\n"
    "Every request a person makes, wherever they make it (a note, a reply, a chat message, an email), "
    "belongs in What You Asked. When you see one that is not listed, record it with record_ask in "
    "their exact words before you work on it.\n\n"
    "Everything you need from a person is a Quest ask, never a line in your result. Raise each "
    "question, approval, choice or action for them with create_decision on this quest, one per ask. "
    "Make each one doable without thinking: the link to open, the command to paste, the message to "
    "send and who to send it to, and the options when it is a choice. Your result may point to the "
    "asks you raised, and must not ask for anything that is not one.\n\n"
    "How to call each of these (create_decision, record_ask, update_ask, update_outcome_progress and "
    "the rest) is listed with its exact command in your brief under TOOLS YOU CAN CALL. For anything "
    "not listed, run `quest-ai-runner quest --list`, then `quest-ai-runner quest --describe NAME`.\n\n"
    "Create goals only where they are missing. Link each to a measurable outcome with outcome_id and "
    "carry that outcome's number and unit into it, set its period and parent goal, and give it a "
    "completion criterion a person can check without asking you. Where a longer horizon has goals and "
    "the ones under it are empty, fill that gap only as far down as the person actually plans.\n\n"
    "Keep the quest's goals current as work moves. When your work spawns more work, start it with "
    "create_task and say which goal it serves: an existing goal with related_goal_id (read the "
    "quest's goals first and use the one that already stands for the work), or a new goal with "
    "new_goal (title, assignee, deadline, outcome_id), which nests under the goal you are working on. "
    "When work rolls over to a teammate, hand the goal over with assign_goal and give the reason and "
    "what they do first: it lands on their goal list with that note as its latest update, their AI "
    "guides them from it, and the goal's open tasks move to their lane. Never leave spawned or "
    "handed-over work without a goal.\n\n"
    "The outcome, measurable outcomes and preferences are the person's own words. Do not rewrite "
    "them, or propose rewriting them, unless the person asks, and change preferences only from what "
    "the person said themselves. When current state no longer matches what you found, raise an ask "
    "with the text it holds now and your full replacement.\n\n"
    "For each measurable outcome your work moved, record progress with update_outcome_progress, "
    "judged honestly: never inflate, keep 100 for an outcome truly met, and make the note name "
    "concrete evidence such as a goal, file, metric or result. A write that changes no figure is "
    "refused, so when a counted target has no real new figure, leave it and name the number you "
    "need. A refusal says why: rewrite it with new "
    "specifics, and after three refusals put where the outcome stands in a quest note instead.\n\n"
    "Carry each running experiment forward: its next step, the data still to collect, and any check "
    "that is due, doing the part that is yours. Never start, change, conclude or spend money on one. "
    "Where the quest has none and your brief says you may plan, propose one as a single ask with its "
    "hypothesis, metric, minimal design, cost and the decision each result would lead to.\n\n"
    "Ground every claim about where things stand in the quest's own fields, goals, notes and files, "
    "never in this prompt. If the plan has slipped, say so and say what you would change.\n\n"
    "Before writing a file into the quest folder, refine the one that already answers it (an "
    "`ai_driven/source_of_truth/` file, or one the person has worked with) instead of writing "
    "another. Keep drafts and dated one-offs in a subfolder.\n\n"
    "Unless the input was only a question, improve at least one solution in each run. Find the "
    "solution your work serves in the quest's or the top intent's specification (get_spec), pick "
    "one of its implementations or add an alternative, measure it with the solution's metric, and "
    "keep whichever measures best. If the specification has a gap that stops you (no solution, "
    "ideal, metric or implementation), fill it yourself with a proposal, never over a field the "
    "person edited. Name the solution, what you changed and the measured result in your result. "
    "Run to the metric run policy: paid runs stay inside the approved budget.\n\n"
    "Your result is the finished thing, not a report about doing it: short enough to read on a phone, "
    "in Markdown, with no preamble and never an em dash. End with what is left and the asks you "
    "raised."
)


# WHAT to work on. The default for a character's OWN ``instructions`` on this quest, so it is the
# job description a run gets when nobody has written one for that character.
BUNDLED_DEFAULT_PERSONA_INSTRUCTIONS = (
    "Work this quest today: pick the one or two things that most move it forward right now, and do "
    "them.\n\n"
    "Judge from the quest whether your role is to do the work yourself (a draft, a comparison, a "
    "decision worked through with its options) or to set the person up to do it (the state of things "
    "read back clearly, the next steps laid out), rather than defaulting to one."
)


# How many of a horizon's current goals the goal ladder lists before it says "+N more". Eight is
# chosen against a real quest that carries around twenty goals in one month: enough that a person
# recognizes their own plan in the list, few enough that the ladder stays context rather than
# becoming the bulk of the prompt. Overridable per call (``current_goal_ladder``).
DEFAULT_LADDER_PER_SCOPE = 8


# How each horizon is named in the ladder. The period id rides alongside in parentheses, so the
# reader gets both the word they think in and the exact period the goals are filed under.
_LADDER_SCOPE_LABELS = {
    "day": "Today",
    "week": "This week",
    "month": "This month",
    "quarter": "This quarter",
    "year": "This year",
}


# Leading Markdown "furniture" stripped off the first line of a person's instructions when it
# becomes a batch's title (headings, bullets, blockquotes) -- a title should read as a title, not
# carry the markup that made sense inside the instructions block itself.
_MD_TITLE_FURNITURE_RE = re.compile(r"^[#\-*>\s]+")


def _goal_name(goal: Dict[str, Any]) -> str:
    return str(goal.get("name") or "").strip() or "(untitled goal)"
