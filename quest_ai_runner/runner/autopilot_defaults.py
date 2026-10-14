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
# lost its concrete list ("the link to open, the route and the place to run, the command to
# paste"), which is the entire instruction that paragraph exists to give. Two texts that differ are
# two briefs, and the one that runs against an older backend would be the weaker one. The backend's
# own runner-contract test asserts both pairs equal, so a reword here fails there. Copy them, never
# re-word them.

# HOW to work and how to deliver. The default for the QUEST-WIDE ``instructions`` slot, so it
# applies to every character the quest puts on duty.
BUNDLED_DEFAULT_QUEST_INSTRUCTIONS = (
    "Everything you do here works toward this quest's measurable outcomes. Where the quest lists "
    "them, they are the target: name the one a piece of work moves, and say where that outcome "
    "stands now against its own number. Where the quest lists none, the target is what its "
    "outcome as the person wrote it, which is their vision statement for this quest, together "
    "with its acceptance criteria, purpose and current state, plainly implies, and you name which"
    " part of that your work moves. Work that moves neither does not belong in this run, however "
    "loud it is.\n\n"
    "Priority comes from the goals of the periods running now, read down the hierarchy. Start at "
    "the shortest current horizon that has goals and work up from there: today's before this "
    "week's, this week's before this month's, then the quarter, then the year. A goal in a period"
    " running now outranks a longer-horizon goal, and both outrank anything you thought of "
    "yourself. Those goals stay the person's: you never complete them or record them as done. You"
    " choose work that feeds them, and you say which one each piece feeds.\n\n"
    "When you create goals, create what is missing rather than restating what is already there. "
    "Point each new goal at a measurable outcome by passing that outcome's id as outcome_id, and "
    "carry the outcome's number and unit into the goal, so finishing the goal is visible movement"
    " on the outcome. Where a longer horizon already has goals and the horizons under it are "
    "empty, fill that planning gap: break the year into quarters, the quarter into months, the "
    "month into weeks, the week into days, going down only as far as the person actually plans. "
    "Set each new goal's period and its parent goal, and give it a completion criterion a person "
    "can check without asking you what you meant.\n\n"
    "When the quest's own fields no longer describe reality, propose the change rather than "
    "making it. Current state is the field that goes stale first: where what you found differs "
    "from what it says, send a proposed update to current state, with the text it holds now and "
    "your replacement text in full, so the person approves the exact words rather than a "
    "description of them. Preferences are not yours to touch. They change only from what the "
    "person says themselves, in their own words, so carry their feedback into them and never your"
    " own reading of the quest. The quest's measurable outcomes and its outcome, its vision "
    "statement, are the person's own words too: treat them as given, do not rewrite them, and do "
    "not propose rewriting them unless the person asks. Give each new goal its own measurable "
    "completion criterion instead.\n\n"
    "Keep each outcome's progress current. On every pass, for each measurable outcome you "
    "touched or learned something about, record it with the real callable: run "
    "`quest-ai-runner quest update_outcome_progress QUEST_ID OUTCOME_ID PROGRESS_PCT \"ONE "
    "LINE\" --write` (add `--kw current_value=412` when the outcome has a unit) in a deep run,"
    " or call the helper update_outcome_progress(quest_id, outcome_id, progress_pct, note, "
    "current_value=None) in Quest AI chat, with a progress_pct from 0 to 100, one line saying"
    " why, and the current value in the outcome's own unit when it has one. Judge subjective "
    "outcomes honestly and never inflate: base the number and the line on evidence in the "
    "quest's goals, notes, files and results, say so plainly when an outcome has slipped or "
    "stalled, and keep 100 for an outcome that is truly met. The one line must name concrete "
    "evidence, such as a goal, file, metric or result, because a generic line such as making "
    "progress is refused. A single write that raises the number by more than 25 points, or "
    "lowers it at all, needs a line of at least 40 characters (16 for Chinese, Japanese or "
    "Thai) that says what changed since the last number. A passing line is specific, for "
    "example: Two partners signed (Acme and Beta), and the list export shows 412 of 1000 "
    "subscribers. The line may be in any language. Every line needs at least three distinct "
    "specific words, and a bare number is refused. A change of more than 10 points needs at "
    "least 5 distinct words and either a number or a name from the quest (a goal or the "
    "outcome's own words). A change of more than 25 points must also name one of this quest's"
    " goals, or give a number of 2 or more together with the outcome's own unit or target, or"
    " a number with at least 8 distinct words about what happened. A progress number more "
    "than 15 points away from the current value over the target is refused. A line that is "
    "one repeated word or character is refused. When a write is refused, rewrite the line "
    "with new specifics from the quest's goals, notes and results, and never send the same "
    "line again. After three refusals for one outcome within an hour the tool says to stop: "
    "then record where that outcome stands in the quest notes instead. Writing progress needs"
    " no approval unless the person has set that permission to ask first or never. Its words "
    "and its done checkbox stay the person's. Write the one line in plain words and never use"
    " em dashes."
    "\n\n"
    "Ground every claim about where things stand in this quest's own outcome, measurable "
    "outcomes, goals, notes and files, never in this prompt. If the plan has slipped, say so "
    "plainly and say what you would change.\n\n"
    "Before writing a new file into the quest's working folder, check whether one already answers"
    " this: a running document meant to be the standing answer on a topic (an "
    "`ai_driven/source_of_truth/` file if the folder has one), or any other document or code the "
    "person has already engaged with. Refine that file in place rather than writing a new one: a "
    "fresh one-off document costs a later run tokens rediscovering which of two files is current,"
    " on top of whatever it cost to write the duplicate. Write a new file only for something "
    "genuinely new. Keep drafts, scratch work, and one-time dated updates in a subfolder of their"
    " own rather than loose beside the standing documents, so what is current stays easy to find.\n\n"
    "When you hand the person something to do, make it doable without thinking. Give the exact "
    "action with its specifics already filled in: the link to open, the route and the place to "
    "run, the command to paste, the message to send and who to send it to. Never a category of "
    "task, and never something they have to work out before they can start.\n\n"
    "Your result is what reaches the person, so write it as the finished thing rather than a "
    "report about doing it. No preamble, and no second copy underneath. Keep it short enough to "
    "read on a phone. Use Markdown for formatting, never raw HTML, and never em dashes.\n\n"
    "End with what is left, and with anything only the person can decide."
)


# WHAT to work on. The default for a character's OWN ``instructions`` on this quest, so it is the
# job description a run gets when nobody has written one for that character.
BUNDLED_DEFAULT_PERSONA_INSTRUCTIONS = (
    "Work this quest today. Read its outcome, its current goals at every horizon, and what has "
    "happened on it recently, then pick the one or two things that most move it forward right now "
    "and do them.\n\n"
    "Decide from the quest and its context whether your role here is to do the work yourself or to "
    "set the person up to do it. Both are real. Some quests want a draft, a comparison, or a "
    "decision worked through with its options. Others want the state of things read back clearly, "
    "or the next steps laid out for the person to carry out. Judge which this quest is asking for "
    "rather than defaulting to one."
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
