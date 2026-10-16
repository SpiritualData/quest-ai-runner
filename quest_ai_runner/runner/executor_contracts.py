"""Text the executor adds to a run: contracts, run history and goal-note rendering.

Split out of ``executor.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..core.reader_first import READER_FIRST_STANDARD


log = logging.getLogger("quest-ai-runner.executor")


# How often (seconds of real time) the throttled cancel_check built by ``_build_cancel_check`` is
# allowed to actually call the Quest API. The orchestrator may poll cancel_check at every loop
# boundary (frequently), but cancellation is a rare, human-triggered event -- hammering the API on
# every check would waste calls for no benefit.
CANCEL_CHECK_INTERVAL_SECONDS = 15.0


# How many of a goal's most recent notes ride along as run context, and how many of the PERSON's
# own notes are guaranteed a place among them (see ``render_goal_notes``).
NOTE_CONTEXT_LIMIT = 8


PERSON_NOTE_FLOOR = 3


# How many rows of a goal's OWN update thread (see ``render_goal_updates``) ride along as run
# context. This is a separate channel from the quest-wide notes above -- a goal update is written
# against one specific goal in the Quest product, so a person's reading notes / progress log on
# that goal live here, not on the quest's notes -- and gets its own small cap rather than sharing
# NOTE_CONTEXT_LIMIT, since it is a per-goal thread and typically much shorter.
GOAL_UPDATE_CONTEXT_LIMIT = 5


def email_contract(quest_id: str, rep_id: Optional[str] = None) -> str:
    """Told to a run whose quest has email switched on.

    Two failures this heads off. A run that mails through a local script sends something Quest
    never saw: no per-quest Reply-To, so the person's answer goes nowhere; no unsubscribe handling;
    no record; and a signature naming a generic assistant rather than the persona that did the
    work. And with delivery now automatic, a run that ALSO mails by hand sends the person the same
    thing twice.
    """
    rep = f" --rep {rep_id}" if rep_id else ""
    return (
        "Email for this quest is ON. What you put in your result is mailed to its people "
        "automatically, so do NOT send mail yourself with a local mail script -- that mail would "
        "carry no reply address, and the person would receive your work twice. The same goes for the "
        "command below: never use it to send the work your result already carries. Only if you "
        "need to send something DIFFERENT at a particular moment instead of at the end, run:\n"
        f"  python -m quest_ai_runner.tools.send_quest_email --quest {quest_id} "
        f"--subject \"<subject>\" --body-file <path>{rep}\n"
        "Recipients come from the quest's own settings: you choose the words and the moment, never "
        "the audience.\n"
        "Write it exactly as you would with email off (see the result contract above): the result "
        "IS the mail, so a \"here is what I sent\" preamble, or a second copy pasted underneath, "
        "is something a person reads in their inbox and has to skip past."
    )


# Appended to the TASK TEXT itself (not only the context) when the quest mails its work. The
# context's email contract reaches the worker, but the goal loop's verifier judges the task's own
# words, and a brief that says "... and email it to him" was held to that literally.
# INCIDENT (2026-09-25): an autopilot persona brief opening "Produce Joshua's dissertation brief
# for TODAY and email it to him" made every work task run TWICE: the first attempt wrote the brief
# as its result (correct: the result is the mail), the verifier ruled it unmet because nothing
# had been emailed, and the retry sent it by hand -- so the person got the hand-sent copy AND the
# automatic one. Stating delivery in the task text is what both the worker and the verifier read.
EARLIER_RUN_EVENT_LIMIT = 25


EARLIER_RUN_TEXT_CHARS = 240


EARLIER_RUN_RESULT_CHARS = 1500


def earlier_run_block(task: Dict[str, Any], resume_session_id: Optional[str] = None) -> str:
    """What a re-run of a task is told about the run before it, or "" for a first run.

    Same material the person sees on the task: how the earlier run ended (its stored result, which
    carries the stop or failure reason, e.g. a watchdog stop) and the tail of its progress feed.
    A re-queued task otherwise starts blind, so a resumed session never learns why it was stopped.
    """
    prior = str(task.get("result") or "").strip()
    events = [e for e in (task.get("progress") or []) if isinstance(e, dict)]
    if not (resume_session_id or (prior and any(e.get("kind") == "started" for e in events))):
        return ""
    lines = []
    for e in events[-EARLIER_RUN_EVENT_LIMIT:]:
        txt = " ".join(str(e.get("text") or "").split())
        if not txt or e.get("kind") == "understanding":
            continue
        lines.append(f"- {str(e.get('at') or '')[:19]} [{e.get('kind')}] {txt[:EARLIER_RUN_TEXT_CHARS]}")
    parts = ["EARLIER RUN OF THIS TASK (read first): this task already ran and was run again. "
             "Continue from where it left off; do not redo finished work, and check files and "
             "notes it already wrote before writing more."]
    if resume_session_id:
        parts.append("You hold that run's own session, so what it read and did is already in context.")
    if prior:
        parts.append(f"How it ended:\n{prior[:EARLIER_RUN_RESULT_CHARS]}")
    if lines:
        parts.append("Its recent progress feed, as the person sees it:\n" + "\n".join(lines))
    return "\n\n".join(parts)


AUTOMATIC_DELIVERY_NOTE = (
    "DELIVERY (set by this quest, not by you): when this run finishes, your result is emailed to "
    "the quest's people automatically. Any instruction above to email, send, or deliver the work "
    "to them is fulfilled by putting the finished work in your result. Do not send it yourself, "
    "and it is not left undone because you did not. This covers the quest's own people only: an "
    "instruction to send a message to a named person by calling a send tool with that recipient "
    "spelled out is a different message to a different reader, so carry that one out with the "
    "tool as written."
)


# Said to EVERY run, whether or not its quest mails anything. The result is the one place the
# person actually reads, so what goes in it cannot depend on a delivery setting the run happens to
# see: the same piece of work is read on the quest, rolled up onto the autopilot pass that created
# it, and mailed where mail is on. Delivery is code's job; the run's job is to write the thing.
#
# The failure this heads off is a result that describes work stored elsewhere ("brief written, see
# the doc"), which leaves Quest holding a pointer instead of the work, and the person with nothing
# to read or reply to.
RESULT_IS_THE_WORK_CONTRACT = (
    "Your result is the one thing a person actually reads: it lands on their quest, it is what an "
    "autopilot pass reports as the work it did, and where a quest mails its work it is mailed word "
    "for word. So put the finished thing IN it, in markdown, written to them: the brief, the "
    "draft, the answer, the decision and why. Say plainly what you did and what is still open. Do "
    "not open with a status banner, do not paste a second copy of anything, and never leave the "
    "real content somewhere else with only a pointer to it here.\n\n"
    + READER_FIRST_STANDARD
)


# How a good colleague behaves around a question they had to ask. Raising one is not a reason to
# stop: they get on with everything that does not depend on the answer, and say what is waiting on
# it. A run that downs tools the moment it needs something turns one unanswered message into a
# stalled week, and the person finds out only when nothing has moved.
#
# The distinction that matters when the answer still has not come: ASKING again is right, FILING
# again is not. Someone who has not acted needs reminding, so the same ask belongs in tomorrow's
# brief; but a second decision-request for it leaves two rows to resolve for one question, and the
# queue fills with duplicates of a thing they have already seen.
KEEP_GOING_CONTRACT = (
    "If you raise a question, need a decision, or hit a blocker: do NOT stop there. Carry on with "
    "everything that does not depend on the answer, and finish by naming what is still waiting on "
    "them and what you did in the meantime. If an earlier run already asked for the same thing and "
    "it is still unanswered, SAY SO AGAIN in what you produce -- someone who has not got to it "
    "needs reminding, not silence -- but do NOT file a second request for it: refer to the one "
    "already open instead of creating a duplicate.\n"
    "The same holds for anything you asked THEM to do. An outstanding item stays outstanding: name "
    "it first, say how long it has been waiting, and do not quietly replace it with today's new "
    "thing. Moving on silently is how the first item is missed entirely -- they cannot chase what "
    "you stopped mentioning."
)


# Stated to any run that can see the person's own words, because the reply loop only closes if
# both halves hold: the run has to answer what they said, and it has to leave the thing they can
# answer NEXT time where they will find it.
#
# The second half is the one that silently fails. A run that mails a brief, writes a file or posts
# elsewhere leaves Quest holding a DESCRIPTION of the work ("sent the brief, id 951f6..."), so the
# person opens their quest to reply and there is nothing there to reply to -- the content they
# actually read lives in their inbox. Quest is where they answer, so Quest gets the real copy.
REPLY_LOOP_CONTRACT = (
    "Closing the loop with the person: if any of the notes or captures above are theirs, open your "
    "result by saying what you did about them, so they can tell their reply landed. And when you "
    "produce something for them to READ (a brief, a summary, a document, an email), put its full "
    "text in your result, not a description of it -- Quest is where they see and answer your work, "
    "so anything you send or write elsewhere is a copy of what belongs there."
)


# What earlier runs did, INCLUDING the ones that failed. A failed run is not an empty one: it may
# have written files, sent mail, or resolved half the work before its goal could be confirmed, and
# the next run that cannot see that redoes it, contradicts it, or reports it as still pending.
RUN_HISTORY_HEADER = (
    "Earlier runs on this quest, oldest first. `failed` means the goal could not be CONFIRMED, not "
    "that nothing happened -- a failed run may have written files, sent mail, or finished most of "
    "the work. Read what it actually did before repeating or contradicting it."
)


# The person is not a subroutine: asking them to do something does not make it done.
NO_ASSUMED_PROGRESS_CONTRACT = (
    "Do NOT assume the person did anything you asked for previously. Confirm it from evidence: a "
    "goal or habit marked done, a note in their own words, or a change you can actually see in "
    "their files. With no evidence, treat it as NOT done, say so plainly, and carry it forward "
    "rather than moving on as though it happened."
)


def render_run_history(tasks: Optional[List[Dict[str, Any]]], limit: int = 6) -> str:
    """Recent runs on this quest, with what each one produced."""
    rows = [t for t in (tasks or []) if (t or {}).get("status") in
            ("done", "failed", "incomplete", "needs_you", "in_progress")]
    rows = sorted(rows, key=lambda t: str(t.get("updated_at") or ""))[-limit:]
    if not rows:
        return ""
    lines = []
    for task in rows:
        when = str(task.get("updated_at") or "")[:10]
        title = (task.get("title") or (task.get("text") or "")[:60] or "task").strip()
        result = " ".join(str(task.get("result") or "").split())[:300]
        line = f"  • [{when}] {task.get('status')}: {title}"
        if result:
            line += f"\n      produced: {result}"
        lines.append(line)
    return f"{RUN_HISTORY_HEADER}\n" + "\n".join(lines)


def render_goal_notes(notes: Optional[List[Dict[str, Any]]]) -> str:
    """Render a goal's recent notes for a run, saying WHO wrote each one.

    This is the reply channel. A person reads what a run produced, answers on the goal ("did the
    reading, skipped the writing, do X instead"), and the next run has to act on that. Two things
    broke it, and both are fixed here:

    1. **Attribution was dropped.** Notes went into the prompt as a flat bullet list, so a run could
       not tell the person's instructions from its OWN previous output. On a goal where the runner
       writes a note every day, that means a run reads a dozen of its own summaries, cannot see
       which one line came from the human, and treats a correction as just more of its own prior
       reasoning. Each note now carries its author and date, and human notes are marked as the
       person's own words, which is the whole point of a reply.

    2. **The person's note fell out of the window.** A plain "most recent N" cut is dominated by AI
       notes precisely on the goals that run often, so yesterday's human correction ages out while
       five machine summaries stay. The most recent ``PERSON_NOTE_FLOOR`` human-authored notes are
       therefore kept regardless of where they land in the ordering.

    Authorship comes from the backend's ``author_kind`` (``"user"`` for a signed-in human,
    ``"ai"`` for an API-key caller). A note with no ``author_kind`` at all is left unlabeled rather
    than guessed at: an older backend, or a note predating attribution, must not be asserted to be
    the person's instruction.
    """
    rows = [n for n in (notes or []) if (n or {}).get("text")]
    if not rows:
        return ""

    def is_person(note: Dict[str, Any]) -> bool:
        return str(note.get("author_kind") or "").lower() == "user"

    kept = rows[-NOTE_CONTEXT_LIMIT:]
    if len(rows) > len(kept):
        floored = [n for n in rows if is_person(n)][-PERSON_NOTE_FLOOR:]
        missing = [n for n in floored if n not in kept]
        if missing:
            # Oldest-to-newest overall, so the run still reads them in chronological order.
            kept = [n for n in rows if n in missing or n in kept]

    lines, retrievable = [], False
    for note in kept:
        kind = str(note.get("author_kind") or "").lower()
        name = str(note.get("author_name") or "").strip()
        if kind == "user":
            who = f"{name}, the person" if name else "the person"
        elif kind == "ai":
            who = f"{name} (AI)" if name and name.lower() != "ai assistant" else "AI"
        else:
            who = name or "unattributed"
        if str(note.get("source") or "").lower() == "email":
            who += ", by email"
        if note.get("source_label"):
            # What the note is from, per the backend (e.g. "Joshua's Daily Reflection"); absent on
            # a plain note and on an older backend.
            who += f", from {str(note['source_label']).strip()}"
        when = str(note.get("created_at") or "")[:10]
        stamp = f"[{when}] " if when else ""
        answering = describe_reply_target(note)
        retrievable = retrievable or bool((note.get("in_reply_to") or {}).get("task_id"))
        lines.append(f"  • {stamp}({who}){answering} {note['text']}")

    header = ("Goal notes, oldest first. Notes marked \"the person\" are the goal owner's own "
              "words: treat them as instructions that override anything an AI note claims.")
    if retrievable:
        header += (" A note that answers one of your emails names that message and the task that "
                   "produced it. A short reply (\"yes, do that\", \"skip it\") means nothing "
                   "without what it answers, so read the original in full with "
                   "query({kind: \"task_history\", task_id: \"<id>\"}) before acting on one, "
                   "rather than guessing which message it meant.")
    return f"{header}\n" + "\n".join(lines)


def describe_reply_target(note: Dict[str, Any]) -> str:
    """What this note is answering, as a phrase to sit after the author, or "".

    Two carriers, deliberately unequal. ``in_reply_to`` is CERTAIN: the reply address the person
    wrote to named that message, so the backend resolved it to the task that produced it and the
    run can read that task in full. ``source_subject`` is only what the subject line said, which
    survives a forward the ref did not and covers mail sent before refs existed, but names a thread
    rather than a message.

    They are worded differently on purpose. A run that cannot tell "this answers task X" from
    "this arrived under some subject" will treat the second as though it could fetch something,
    and the whole point of the certain path is that it never has to guess.
    """
    target = note.get("in_reply_to") or {}
    task_id = target.get("task_id")
    if task_id:
        called = (target.get("title") or target.get("subject") or "").strip()
        sent = str(target.get("sent_at") or "")[:10]
        parts = [f'"{called}"' if called else "an earlier message"]
        if sent:
            parts.append(f"sent {sent}")
        parts.append(f"task {task_id}")
        return f" answering {parts[0]} ({', '.join(parts[1:])}):" if len(parts) > 1 \
            else f" answering {parts[0]}:"
    subject = (note.get("source_subject") or "").strip()
    if subject:
        # No task to read: the subject is a name, not a handle. Said plainly so a run does not go
        # looking for something to fetch.
        return f' under the subject "{subject}" (the message itself is not linked):'
    return ""


def render_goal_updates(updates: Optional[List[Dict[str, Any]]]) -> str:
    """Render THIS goal's own update thread -- the person's check-in notes on this one goal.

    A goal update is a different channel from ``render_goal_notes`` above: notes are the QUEST's
    reply thread (both sides, whichever goal a note happens to be about, if any); an update is
    written against exactly one goal in the Quest product, so it is where a person's reading notes
    or progress log on THAT goal live. Nothing else in this library reads them, so a run working a
    goal has never been able to see the person's own words about it -- only ``Goal description``,
    which says what the goal IS, never what they've actually done or found. The header below says
    so plainly, because a run that does not know this channel exists will keep relying on the
    description alone.

    Newest first, matching the client's own ordering (unlike the notes thread, which is
    chronological) -- a person skimming a handful of their own check-ins wants the latest one
    first. FULL text, never summarized or truncated: these are the person's own words, and a
    summary here would be exactly the kind of afterthought this channel exists to avoid.
    """
    rows = [u for u in (updates or []) if (u or {}).get("note")]
    if not rows:
        return ""
    rows = rows[:GOAL_UPDATE_CONTEXT_LIMIT]
    lines = []
    for update in rows:
        when = str(update.get("createdAt") or "")[:10]
        who = str(update.get("userName") or "").strip() or "the goal owner"
        stamp = f"[{when}] " if when else ""
        lines.append(f"  • {stamp}({who}) {update['note']}")
    header = ("Goal updates -- what the person themselves wrote on THIS goal's own check-in "
              "thread, newest first. This is not the goal description above: it is their own "
              "account, in their own words, of what they actually did or found on this goal. "
              "Read it the same way you would a note addressed to you.")
    return f"{header}\n" + "\n".join(lines)
