"""quest_experiments -- a quest's experiments as a core input of the runner.

Quest keeps a quest's experiments as *research questions* (``GET /api/quests/{id}/research-questions``;
the model is quest-backend ``app/models/research_question.py``). This module is the ONE place the
runner turns those rows into text, for two readers:

* the quest folder: :func:`render_experiments_block` is every field the API exposes, written into
  the ``QAR:MANAGED:experiments`` block of ``QUEST_SYNC.md`` by ``quest_folder_sync`` (pull-only);
* a model prompt: :func:`render_experiments_brief` is the compact, budgeted form, and
  :func:`experiments_brief_section` wraps it with what the run should DO about experiments. The
  autopilot composes it into every batch (``autopilot_briefs.compose_batch_text``).

What counts as "an experiment". Every quest has one DEFAULT row (``kind == "default"``), the
question its creation inquiry asked; Quest materialises it on the first read. It is the quest's own
research question and not something the person set up, so a quest whose only row is an untouched
default row has NO experiment for the purposes of "carry it forward / propose one"
(:func:`has_experiment`). The decision is made on structured fields (``kind``, ``status``, links,
analyses), never on the wording of any text.

Proposals. The runner never starts an experiment. A quest with none gets a PROPOSAL: one open
decision-request of kind :data:`EXPERIMENT_PROPOSAL_KIND`. "At most one open per quest" is enforced
in code where the decision is created (``QuestClient.create_decision``), so it holds however many
runs try, and :func:`proposal_is_open` tells the composer to stop asking while one is waiting.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional

from ._managed_sections import strip_managed_blocks

log = logging.getLogger("quest-ai-runner.quest_experiments")

EXPERIMENTS_START = "<!-- QAR:MANAGED:experiments START -->"
EXPERIMENTS_END = "<!-- QAR:MANAGED:experiments END -->"

#: The ``kind`` of the decision-request that proposes an experiment. Part of the on-server contract:
#: the one-open-proposal rule and the "a proposal is waiting" line both find it by this value.
EXPERIMENT_PROPOSAL_KIND = "experiment_proposal"

KIND_DEFAULT = "default"
STATUS_OPEN = "open"
STATUS_EXPERIMENTING = "experimenting"
STATUS_CONCLUDED = "concluded"

#: Characters the compact prompt form may use. About 1000 tokens: enough for a few running
#: experiments in detail, small enough that it never crowds out the quest's own brief.
EXPERIMENTS_BRIEF_MAX_CHARS = 4000

QUEST_SYNC_FILE = "QUEST_SYNC.md"


# --- reading ---------------------------------------------------------------------------

def fetch_experiments(client: Any, quest_id: str) -> Optional[List[Dict[str, Any]]]:
    """The quest's experiment rows, or ``None`` when they could not be read.

    ``None`` and ``[]`` are different answers and callers must keep them apart: ``[]`` is "Quest
    says there are none", ``None`` is "this client cannot ask, or the ask failed". Treating a failed
    read as "no experiments" would tell a run to propose an experiment on a quest that has three.
    Never raises.
    """
    lister = getattr(client, "list_experiments", None)
    if not callable(lister):
        return None
    try:
        rows = lister(quest_id)
    except Exception as e:  # noqa: BLE001 -- experiments are additive; never block a sync or a pass
        log.info("could not read experiments for quest %s (%s: %s)", quest_id, type(e).__name__, e)
        return None
    return [r for r in (rows or []) if isinstance(r, dict)]


def proposal_is_open(client: Any, quest_id: str) -> bool:
    """Whether an experiment proposal is already waiting on the person for this quest.

    Fails CLOSED (reports ``True``) when the open decisions cannot be read: the cost of a missed
    proposal is one quiet day, the cost of a duplicate is the interruption stream this rule exists
    to prevent. A client with no such listing has nothing to dedupe against and reports ``False``.
    """
    lister = getattr(client, "list_open_decisions_for_quest", None)
    if not callable(lister):
        return False
    try:
        return any(str(d.get("kind") or "") == EXPERIMENT_PROPOSAL_KIND for d in (lister(quest_id) or []))
    except Exception:  # noqa: BLE001
        log.info("could not read open decisions for quest %s; treating a proposal as open",
                 quest_id, exc_info=True)
        return True


def is_default(exp: Dict[str, Any]) -> bool:
    return str(exp.get("kind") or "") == KIND_DEFAULT


def is_running(exp: Dict[str, Any]) -> bool:
    return str(exp.get("status") or "") == STATUS_EXPERIMENTING


def is_engaged(exp: Dict[str, Any]) -> bool:
    """Whether the person (or a run on their behalf) has actually done anything with this row.

    A custom row always counts. The default row counts only once it has moved off ``open`` or been
    given data (links, analyses, a conclusion): until then it is just the question creation asked.
    """
    if not is_default(exp):
        return True
    return bool(str(exp.get("status") or STATUS_OPEN) != STATUS_OPEN
                or exp.get("linked_goal_ids") or exp.get("linked_collection_ids")
                or exp.get("analyses") or str(exp.get("conclusion") or "").strip())


def has_experiment(experiments: Iterable[Dict[str, Any]]) -> bool:
    return any(is_engaged(e) for e in experiments)


# --- small text helpers ----------------------------------------------------------------

def clean_text(value: Any) -> str:
    """Caller text made safe to sit inside a managed block (no marker can nest)."""
    return strip_managed_blocks(str(value or "")).strip()


def one_line(value: Any, limit: int = 0) -> str:
    out = " ".join(clean_text(value).split())
    if limit and len(out) > limit:
        out = out[: max(0, limit - 3)].rstrip() + "..."
    return out


def block_quote(value: Any) -> List[str]:
    """Multi-line text as a Markdown block quote, so its own headings and lists stay inert."""
    return [f"> {ln}" if ln.strip() else ">" for ln in clean_text(value).splitlines()]


def day_of(value: Any) -> str:
    return str(value or "")[:10]


def status_label(exp: Dict[str, Any]) -> str:
    status = str(exp.get("status") or STATUS_OPEN)
    return {STATUS_EXPERIMENTING: "experimenting (running, collecting data)",
            STATUS_OPEN: "open (asked, nothing running yet)",
            STATUS_CONCLUDED: "concluded"}.get(status, status)


def hypothesis_lines(exp: Dict[str, Any], limit: int = 0) -> List[str]:
    subs = {str(s.get("sub_question_id")): one_line(s.get("question")) for s in (exp.get("sub_questions") or [])
            if isinstance(s, dict)}
    out = []
    for h in exp.get("hypotheses") or []:
        if not isinstance(h, dict) or not clean_text(h.get("statement")):
            continue
        about = subs.get(str(h.get("sub_question_id") or ""), "")
        out.append(f"{one_line(h.get('statement'), limit)}" + (f" (about: {about})" if about else ""))
    return out


def condition_line(c: Dict[str, Any], limit: int = 0) -> str:
    parts = [f"{one_line(c.get('name')) or c.get('condition_id') or 'condition'}"
             f"{'' if c.get('required', True) else ' (optional)'}",
             f"metric {c.get('metric') or 'days_done'}"]
    if c.get("minimum") is not None:
        parts.append(f"at least {c.get('minimum')} per {c.get('period') or 'week'}")
    if c.get("daily_minimum") is not None:
        parts.append(f"a day counts at {c.get('daily_minimum')}+")
    if c.get("days_of_week"):
        parts.append("weekdays " + ",".join(str(d) for d in c["days_of_week"]) + " (0=Mon)")
    line = "; ".join(parts)
    definition = one_line(c.get("definition"), limit)
    return f"{line}: {definition}" if definition else line


# --- the folder form: every field ------------------------------------------------------

def render_experiment_full(exp: Dict[str, Any]) -> List[str]:
    rid = str(exp.get("research_question_id") or "")
    tag = "quest's own question" if is_default(exp) else "experiment"
    lines = [f"### {one_line(exp.get('question')) or '(no question)'}", "",
             f"- **Id:** `{rid}` ({tag})",
             f"- **Status:** {status_label(exp)}",
             f"- **Owner (asked by user id):** {exp.get('user_id') or '(unknown)'}",
             f"- **Dates:** start {exp.get('start_date') or '(not set)'}, end {exp.get('end_date') or '(not set)'}",
             f"- **Created / updated:** {day_of(exp.get('created_at'))} / {day_of(exp.get('updated_at'))}"]
    if clean_text(exp.get("purpose")):
        lines += ["- **Purpose:**"] + block_quote(exp["purpose"])
    if is_default(exp):
        # Quest builds the default row's context from the quest's own outcome, current state and
        # preferences on every read. Those already sit in the goal block, and the preferences can
        # run to pages, so repeating them here would bury the experiments under a copy of the quest.
        lines.append("- **Context:** derived by Quest from this quest's outcome, current state, "
                     "preferences and timeline (see the goal block above)")
    elif clean_text(exp.get("context")):
        lines += ["- **Context:**"] + block_quote(exp["context"])
    hyps = hypothesis_lines(exp)
    lines.append("- **Hypotheses:** " + ("" if hyps else "(none stated)"))
    lines += [f"  - {h}" for h in hyps]
    subs = [s for s in (exp.get("sub_questions") or []) if isinstance(s, dict) and clean_text(s.get("question"))]
    if subs:
        lines.append("- **Sub-questions:**")
        for s in subs:
            how = one_line(s.get("answer_method"))
            lines.append(f"  - {one_line(s.get('question'))}" + (f" (answer method: {how})" if how else ""))
    assumptions = [a for a in (exp.get("assumptions") or []) if clean_text(a)]
    if assumptions:
        lines.append("- **Assumptions:**")
        lines += [f"  - {one_line(a)}" for a in assumptions]
    conds = [c for c in (exp.get("conditions") or []) if isinstance(c, dict)]
    if conds:
        lines.append("- **Protocol conditions (checked against the entries, not by an AI):**")
        lines += [f"  - {condition_line(c)}" for c in conds]
    goals, colls = exp.get("linked_goal_ids") or [], exp.get("linked_collection_ids") or []
    lines.append("- **Linked goals:** " + (", ".join(f"`{g}`" for g in goals) if goals else "(none)"))
    lines.append("- **Linked habits / data collections:** "
                 + (", ".join(f"`{c}`" for c in colls) if colls else "(none)"))
    inquiries = exp.get("deep_inquiry_ids") or []
    lines.append(f"- **Deep inquiries run:** {len(inquiries)}"
                 + (" (" + ", ".join(f"`{i}`" for i in inquiries) + ")" if inquiries else ""))
    analyses = [a for a in (exp.get("analyses") or []) if isinstance(a, dict)]
    lines.append(f"- **Analyses:** {len(analyses)}" + (" (newest first)" if analyses else ""))
    for n, a in enumerate(reversed(analyses)):
        head = (f"{day_of(a.get('created_at'))}, confidence {a.get('confidence') or 'low'}, "
                f"{a.get('data_points') or 0} data points, window "
                f"{day_of(a.get('data_window_start')) or '?'} to {day_of(a.get('data_window_end')) or '?'}")
        lines.append(f"  - {head}")
        if n:  # older analyses: the answer only; the newest one carries everything
            if clean_text(a.get("answer")):
                lines.append(f"    - Answer: {one_line(a.get('answer'))}")
            continue
        for label, key in (("Answer", "answer"), ("Numbers in words", "quantitative_summary"),
                           ("Notes in words", "qualitative_summary")):
            if clean_text(a.get(key)):
                lines.append(f"    - {label}:")
                lines += ["    " + q for q in block_quote(a[key])]
        for label, key in (("Limitations", "limitations"), ("Suggested next steps", "suggested_next_steps")):
            items = [i for i in (a.get(key) or []) if clean_text(i)]
            if items:
                lines.append(f"    - {label}:")
                lines += [f"      - {one_line(i)}" for i in items]
        if a.get("quantitative"):
            lines.append("    - Computed numbers: `"
                         + json.dumps(a["quantitative"], sort_keys=True, default=str).replace("`", "'") + "`")
    if clean_text(exp.get("conclusion")):
        when = f" (concluded {day_of(exp.get('concluded_at'))})" if exp.get("concluded_at") else ""
        lines += [f"- **Conclusion{when}:**"] + block_quote(exp["conclusion"])
    else:
        lines.append("- **Conclusion:** (none yet)")
    return lines


def render_experiments_block(experiments: List[Dict[str, Any]]) -> str:
    """The body of the ``QAR:MANAGED:experiments`` block: every experiment, every field.

    Running experiments first, then open, then concluded, then the quest's own default question, so
    the first screen is what is being carried out now. The sync pulls this and never pushes it:
    editing the block changes nothing on Quest (change an experiment in Quest, or ask Quest AI).
    """
    lines = ["## Experiments", "",
             "_Experiments are core to this quest. Pulled from Quest on every sync and overwritten, so "
             "never hand-edit inside this block; to change one, use the Experiments section in Quest "
             "or ask Quest AI (this block is pull-only)._"]
    if not experiments:
        return "\n".join(lines + ["", "_(no experiments yet)_"])
    if not has_experiment(experiments):
        lines += ["", "_No experiment has been set up on this quest yet. Only the quest's own research "
                      "question exists (below)._"]
    for exp in ordered_experiments(experiments):
        lines += ["", *render_experiment_full(exp)]
    return "\n".join(lines)


def ordered_experiments(experiments: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def rank(e: Dict[str, Any]) -> int:
        if is_default(e) and not is_engaged(e):
            return 3
        return {STATUS_EXPERIMENTING: 0, STATUS_OPEN: 1, STATUS_CONCLUDED: 2}.get(
            str(e.get("status") or STATUS_OPEN), 1)
    return sorted(experiments, key=rank)  # stable: Quest's own order inside a rank


# --- the prompt form: compact, budgeted -----------------------------------------------

def brief_entry(exp: Dict[str, Any], detail: bool) -> str:
    rid = exp.get("research_question_id") or "?"
    mark = {STATUS_EXPERIMENTING: "RUNNING, help carry it out",
            STATUS_OPEN: "OPEN, not collecting data yet",
            STATUS_CONCLUDED: "CONCLUDED"}.get(str(exp.get("status") or STATUS_OPEN), "OPEN")
    if is_default(exp) and not is_engaged(exp):
        mark = "the quest's own question, untouched"
    head = f"- [{mark}] {one_line(exp.get('question'), 200)} (id {rid})"
    if not detail:
        return head
    bits: List[str] = []
    hyps = hypothesis_lines(exp, 160)
    if hyps:
        bits.append("hypotheses: " + " | ".join(hyps[:3]) + (f" (+{len(hyps) - 3} more)" if len(hyps) > 3 else ""))
    subs = [one_line(q.get("question"), 140) for q in (exp.get("sub_questions") or [])
            if isinstance(q, dict) and clean_text(q.get("question"))]
    if subs:
        bits.append("sub-questions: " + " | ".join(subs[:3]) + (f" (+{len(subs) - 3} more)" if len(subs) > 3 else ""))
    if exp.get("start_date") or exp.get("end_date"):
        bits.append(f"runs {exp.get('start_date') or '?'} to {exp.get('end_date') or '?'}")
    conds = [c for c in (exp.get("conditions") or []) if isinstance(c, dict)]
    if conds:
        bits.append("protocol: " + "; ".join(condition_line(c, 100) for c in conds[:4])
                    + (f" (+{len(conds) - 4} more)" if len(conds) > 4 else ""))
    goals, colls = exp.get("linked_goal_ids") or [], exp.get("linked_collection_ids") or []
    if goals or colls:
        bits.append(f"collecting data through {len(goals)} goal(s) and {len(colls)} habit(s)/collection(s)")
    elif str(exp.get("status") or "") != STATUS_CONCLUDED and not is_default(exp):
        bits.append("nothing is linked to collect its data yet")
    analyses = [a for a in (exp.get("analyses") or []) if isinstance(a, dict)]
    if analyses:
        a = analyses[-1]
        bits.append(f"latest analysis {day_of(a.get('created_at'))}, confidence {a.get('confidence') or 'low'}, "
                    f"{a.get('data_points') or 0} data points: {one_line(a.get('answer'), 240)}")
        steps = [one_line(s, 120) for s in (a.get("suggested_next_steps") or []) if clean_text(s)]
        if steps:
            bits.append("suggested next steps: " + " | ".join(steps[:3]))
    elif exp.get("linked_goal_ids") or exp.get("linked_collection_ids"):
        bits.append("not analysed yet")
    if clean_text(exp.get("conclusion")):
        bits.append("conclusion: " + one_line(exp["conclusion"], 300))
    return head + "".join(f"\n    {b}" for b in bits)


def render_experiments_brief(experiments: List[Dict[str, Any]],
                             max_chars: int = EXPERIMENTS_BRIEF_MAX_CHARS) -> str:
    """The experiments as a compact list for a model prompt, within ``max_chars``.

    Detail is spent where it matters: running and open experiments first and in full, then
    concluded ones and the quest's own question. When the whole does not fit, the later rows drop
    to one line each, then the tail is replaced by a count; the full text is always in
    ``QUEST_SYNC.md``, and the line says so.
    """
    rows = ordered_experiments(experiments)
    if not rows:
        return ""
    pointer = f"(Every field of every experiment is in {QUEST_SYNC_FILE} in the quest folder.)"
    def assemble(details: List[bool], shown: int) -> str:
        body = [brief_entry(e, d) for e, d in zip(rows[:shown], details[:shown])]
        if shown < len(rows):
            body.append(f"- ... and {len(rows) - shown} more, in {QUEST_SYNC_FILE}")
        return "\n".join(body + [pointer])
    live = [is_engaged(e) and str(e.get("status") or STATUS_OPEN) != STATUS_CONCLUDED for e in rows]
    for details in ([True] * len(rows), live, [False] * len(rows)):
        out = assemble(details, len(rows))
        if len(out) <= max_chars:
            return out
    for shown in range(len(rows) - 1, 0, -1):
        out = assemble([False] * len(rows), shown)
        if len(out) <= max_chars:
            return out
    return assemble([False] * len(rows), 1)[:max_chars]


CARRY_FORWARD_TEXT = (
    "Experiments on this quest. Experiments are a core part of how this quest moves forward, so "
    "treat them as part of the work. For each RUNNING one, say what its next step is, what data is "
    "still to be collected and through which goal or habit, and whether a check is due (its "
    "protocol conditions, its end date, an analysis that is overdue), and do the part of that which "
    "is yours to do. For an OPEN one, say what would start it. Do not start, conclude, rewrite or "
    "spend money on an experiment yourself: where one should change, say what and why, and let the "
    "person decide.")

PROPOSE_TEXT = (
    "This quest has no experiment of its own yet, and experiments are core to how a quest learns "
    "what works. Propose ONE experiment for the person to review, only if you can ground it in this "
    "quest's outcome, goals and notes. Write it as a single decision-request holding: the "
    "hypothesis, the metric, the minimal design (what changes, for how long, what is recorded), the "
    "cost in time and money, and the decision that would follow from each result. Raise it with "
    "`quest-ai-runner quest create_decision \"SUMMARY\" --kw kind={kind} --kw quest_id={quest_id} "
    "--write`. Never create or start the experiment: it exists only if the person approves. If you "
    "have no well-grounded experiment to propose, propose none.")

PROPOSAL_WAITING_TEXT = (
    "This quest has no experiment of its own yet, and an experiment proposal is already waiting for "
    "the person's review. Do not raise another.")


def experiments_brief_section(experiments: Optional[List[Dict[str, Any]]], *, quest_id: str,
                              planning: str = "work_only", proposal_open: bool = False,
                              may_propose: bool = True,
                              max_chars: int = EXPERIMENTS_BRIEF_MAX_CHARS) -> str:
    """The experiments section of an autopilot batch: what exists, and what to do about it.

    ``experiments`` is :func:`fetch_experiments`'s answer, ``None`` (unreadable) included: an
    unreadable list yields NO section, because "I could not look" must never be rendered as "there
    are none". The three outcomes for a readable list:

    * the quest has experiments: carry them forward (always, whatever the planning setting, since
      carrying out what the person already set up is work, not planning);
    * it has none and ``planning == "plan_and_work"``: propose one, unless a proposal is already
      open or another batch of this same pass is the one proposing (``may_propose`` False);
    * it has none and the quest is ``work_only``: say nothing. Proposing an experiment is planning
      and the person withheld that.
    """
    if experiments is None:
        return ""
    if has_experiment(experiments):
        return CARRY_FORWARD_TEXT + "\n\n" + render_experiments_brief(experiments, max_chars)
    if planning != "plan_and_work":
        return ""
    if proposal_open or not may_propose:
        return PROPOSAL_WAITING_TEXT
    return PROPOSE_TEXT.format(kind=EXPERIMENT_PROPOSAL_KIND, quest_id=quest_id)
