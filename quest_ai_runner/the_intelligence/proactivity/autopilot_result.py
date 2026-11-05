"""``AutopilotResult`` and the per-quest pass lock.

Split out of ``autopilot.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class AutopilotResult:
    """What one autopilot pass did -- the pass task's own reported result (see ``summary_text``).

    ``created`` holds one dict per item the pass created, and it holds NAMES, not just ids:
    ``{task_id, kind, title, quest_id, quest_label, persona_label, awaiting_approval,
    adopted_titles}``. That is not decoration. This result is read by a person, in
    their quest, and a line like "Created 1 task(s): atask_d2014273cff6" tells them nothing they
    can act on: it names an internal id instead of the work, and it describes the pass's own
    bookkeeping as if it were the point. The point is what is now being worked on, said in the
    words the person themselves gave that work.
    """
    ran_at: datetime
    dry_run: bool = False
    created: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)      # {quest_id, quest_label, reason}
    proposals: List[Dict[str, Any]] = field(default_factory=list)    # dry-run "would create" items
    errors: List[Dict[str, Any]] = field(default_factory=list)       # {quest_id, quest_label, error}
    # Bookkeeping writes the backend ACCEPTED (200) but did not actually persist. Kept separate
    # from ``errors`` because the pass itself succeeded; what failed is the cadence/miss_streak
    # memory, which silently degrades the NEXT pass (a last_pass_at that never sticks means the
    # cadence gate can never fire). Surfaced in the reported summary so it can never pass silently.
    bookkeeping_warnings: List[Dict[str, Any]] = field(default_factory=list)  # {quest_id, detail}
    # Quests whose canonical next-steps artifact this pass rewrote: {quest_id, path, quest_target}.
    next_steps_refreshed: List[Dict[str, Any]] = field(default_factory=list)
    # Pending work counted on a quest this pass ran: {quest_id, quest_label, count, label}. The
    # backlog's size is the metric to watch, so a pass says it every time it is non-zero.
    pending_work: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def created_task_ids(self) -> List[str]:
        """The ids of everything this pass created, for the gates and for callers that link rows.

        Ids belong HERE, in a field code reads, and not in the prose a person reads.
        """
        return [str(c.get("task_id")) for c in self.created if c.get("task_id")]

    def summary_text(self) -> str:
        """What this pass set in motion, in plain words.

        This is what the pass task reports, so it is what a person sees on their quest until the
        work itself finishes -- at which point the consumer replaces it with the work's OWN output
        (see the module docstring). So it says what is running and what is waiting on them, and it
        never presents the pass's own bookkeeping as the result.
        """
        if self.dry_run:
            return self._dry_run_text()
        lines: List[str] = []
        running = [c for c in self.created if not c.get("awaiting_approval")]
        awaiting = [c for c in self.created if c.get("awaiting_approval")]
        if running:
            lines.append(_count_line(len(running), "Autopilot started one piece of work:",
                                     "Autopilot started {n} pieces of work:"))
            lines.extend(_describe_created(c) for c in running)
        # Proposals are separated from work: a proposed goal is not a task queued behind an
        # approval, it is a suggestion to accept or reject, and saying "before it can run" of a
        # goal that does not exist yet describes the wrong thing.
        proposals = [c for c in awaiting if c.get("kind") == "goal_proposal"]
        awaiting_work = [c for c in awaiting if c.get("kind") != "goal_proposal"]
        if awaiting_work:
            if lines:
                lines.append("")
            lines.append(_count_line(len(awaiting_work),
                                     "Waiting for your approval before it can run:",
                                     "Waiting for your approval before they can run:"))
            lines.extend(_describe_created(c) for c in awaiting_work)
        if proposals:
            if lines:
                lines.append("")
            lines.append(_count_line(len(proposals),
                                     "A goal proposed for you to accept or reject:",
                                     "{n} goals proposed for you to accept or reject:"))
            lines.extend(_describe_created(c) for c in proposals)
        if self.pending_work:
            if lines:
                lines.append("")
            for p in self.pending_work:
                lines.append(f"{p.get('quest_label') or p.get('quest_id')}: "
                             f"{p.get('count')} {p.get('label')}.")
        if self.next_steps_refreshed:
            if lines:
                lines.append("")
            lines.append("Next steps rewritten for:")
            for n in self.next_steps_refreshed:
                lines.append(f"  - {n.get('quest_label') or n.get('quest_id')}: {n.get('path')}")
        if self.skipped:
            if lines:
                lines.append("")
            lines.append(_count_line(len(self.skipped), "Nothing started on one quest:",
                                     "Nothing started on {n} quests:"))
            for s in self.skipped:
                lines.append(f"  - {s.get('quest_label') or s.get('quest_id')}: {s.get('reason')}")
        if self.errors:
            if lines:
                lines.append("")
            lines.append(_count_line(len(self.errors), "One quest hit an error:",
                                     "{n} quests hit an error:"))
            for e in self.errors:
                lines.append(f"  - {e.get('quest_label') or e.get('quest_id')}: {e.get('error')}")
        if self.bookkeeping_warnings:
            if lines:
                lines.append("")
            lines.append(
                f"WARNING: autopilot bookkeeping did not persist on "
                f"{len(self.bookkeeping_warnings)} quest(s). The cadence gate reads "
                f"last_pass_at, so until this is fixed those quests are considered due on EVERY "
                f"pass (the per-quest cadence cannot hold them back):")
            for w in self.bookkeeping_warnings:
                label = w.get("quest_label") or w.get("quest_id")
                lines.append(f"  - {label}: {w.get('detail')}")
        if not lines:
            lines.append("No quest has autopilot switched on, so this pass had nothing to work on.")
        return "\n".join(lines)

    def _dry_run_text(self) -> str:
        """The dry run's report: what a real pass WOULD do, nothing created.

        Kept id-bearing where a real pass's report is not: a dry run is read while setting the
        thing up, by someone checking that the right goals and the right recurring tasks were
        picked, and an id is what they check against.
        """
        lines = ["Autopilot dry run: nothing was created. Here is what WOULD happen:"]
        for p in self.proposals:
            label = p.get("quest_label") or p.get("quest_id")
            if p.get("kind") == "goal_proposal":
                lines.append(f"  - Propose a goal on {label}: {p.get('title')}")
                continue
            line = (f"  - Work on {label} as {p.get('persona_label') or 'the assistant'} "
                    f"({p.get('scope')})")
            # Adoption CLOSES the user's own recurring tasks, so a report that omitted it
            # would hide the most consequential thing the pass does.
            adopted = p.get("adopted_task_ids")
            if adopted:
                line += f", adopting and closing recurring task(s) {adopted}"
            # Which brief this run would be working to. On a roster where two characters do
            # different jobs, this is the line that shows the routing landed the right way round.
            instructions_from = p.get("instructions_from")
            if instructions_from:
                line += f", working {instructions_from}"
            lines.append(line)
        for s in self.skipped:
            lines.append(f"  - Skip {s.get('quest_label') or s.get('quest_id')}: {s.get('reason')}")
        for e in self.errors:
            lines.append(f"  - Error on {e.get('quest_label') or e.get('quest_id')}: {e.get('error')}")
        if not (self.proposals or self.skipped or self.errors):
            lines.append("  - Nothing: no quest has autopilot switched on.")
        return "\n".join(lines)


def _count_line(n: int, one: str, many: str) -> str:
    """``one`` when there is exactly one of something, otherwise ``many`` with ``{n}`` filled in.

    A person reading "Created 1 task(s)" is reading a template that was never finished.
    """
    return one if n == 1 else many.format(n=n)


def _quest_label(quest: Dict[str, Any], quest_id: str) -> str:
    """A quest said the way its owner would say it: its outcome, or the id if it has none.

    The outcome is the intent's end state, the sentence the person sees most often on the quest,
    so this is a phrase they recognize (the short ``name`` field is a separate label this report
    does not read). Truncated, because an outcome can be a paragraph and this goes in a one-line
    report.
    """
    label = str(quest.get("outcome") or "").strip().splitlines()[0] if quest.get("outcome") else ""
    if not label:
        return quest_id
    return label if len(label) <= 80 else label[:77].rstrip() + "..."


def _task_label(task: Dict[str, Any]) -> str:
    """A task said by its title, falling back to its first line. Never a bare id."""
    text = str(task.get("title") or task.get("text") or "").strip()
    first = text.splitlines()[0].strip() if text else ""
    if not first:
        return str(task.get("id") or task.get("task_id") or "an unnamed task")
    return first if len(first) <= 80 else first[:77].rstrip() + "..."


def _describe_created(item: Dict[str, Any]) -> str:
    """One created item, described by NAME: the work, whose quest it serves, who is doing it."""
    title = str(item.get("title") or "").strip() or "an unnamed piece of work"
    line = f"  - {title}"
    quest_label = str(item.get("quest_label") or "").strip()
    # Which quest, unless the title already says so. A proposed goal is titled after the quest's
    # own outcome, so naming the quest again gave "Next step toward: <outcome> (on <outcome>)".
    if quest_label and quest_label.lower() not in title.lower():
        line += f" (on {quest_label})"
    persona = str(item.get("persona_label") or "").strip()
    if persona:
        line += f", as {persona}"
    adopted = [t for t in (item.get("adopted_titles") or []) if t]
    if adopted:
        # Adoption takes over tasks the PERSON scheduled and closes the originals, so it can never
        # go unsaid: they would otherwise be waiting for runs that are never going to happen.
        line += (". It also takes over, and closes, the recurring task"
                 f"{'s' if len(adopted) > 1 else ''} you had scheduled: {', '.join(adopted)}")
    return line


# One lock per quest, process-wide. Passes run on the executor's worker threads, so two pass
# occurrences due at the same minute used to run the same quest CONCURRENTLY: both read the quest
# before either stamped ``last_pass_at``, both passed the cadence gate, and each created its own
# batch (INCIDENT 2026-09-25: two duplicate series, two briefs' worth of deep runs, four emails).
# Held around the whole pass, including the quest read in ``_eligible_quests``, so the second
# pass reads the quest AFTER the first has stamped it and is skipped by the ordinary cadence gate.
# The key is the pass's quest id ("" for an unscoped team pass, which serializes those too).
_QUEST_PASS_LOCKS: Dict[str, threading.Lock] = {}


_QUEST_PASS_LOCKS_GUARD = threading.Lock()


def _quest_pass_lock(quest_id: str) -> threading.Lock:
    with _QUEST_PASS_LOCKS_GUARD:
        lock = _QUEST_PASS_LOCKS.get(quest_id)
        if lock is None:
            lock = _QUEST_PASS_LOCKS[quest_id] = threading.Lock()
        return lock
