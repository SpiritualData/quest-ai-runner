"""The progress record and the reply composed from it by code: the deliverable first, the measured increment, the milestone metric read again, then the one person-only step.

Role: ``Progress`` holds what the turn did and renders the kept body and reply text; ``milestone_pass`` turns it into the pass the flow step returns. No model call is made here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from .milestone_facts import FOLLOW_SEPARATOR, OVERVIEW_HEAD, PART_SEPARATOR
from .milestone_rubric import kept_rubric, rubric_entry


@dataclass
class Progress:
    """What the progress step did, for the reply and the turn's record."""
    title: str = ""
    item: str = ""
    overview: str = ""
    parts: List[str] = field(default_factory=list)
    measure_name: str = ""
    unit: str = ""
    before: Optional[float] = None
    after: Optional[float] = None
    total: Optional[int] = None
    as_of: str = ""
    kept_state: str = ""
    metric_name: str = ""
    metric_before: str = ""
    metric_after: str = ""
    ask: str = ""
    child_metric: Optional[Mapping[str, Any]] = None
    status: str = ""
    why: str = ""
    rubric_state: str = ""          # "reused" (kept on the record and read back), "kept" (defined this turn and written), or why it was not kept
    notes: str = ""                 # lines the milestone's parent and dependent solution passes add, after the person-only step
    depth: Optional[Mapping[str, Any]] = None       # the depth loop's account: rubric score of the one-call unit (before) and after depth, calls, levels, why it stopped
    sources: str = ""               # what the writer was given beyond the turn's reads (the research it gathered), kept in the turn's read record
    tier: str = ""                  # the tier the deliverable was written on
    unsupported: List[str] = field(default_factory=list)     # sentences taken out because they stated a fact no source holds
    proposed_units: List[str] = field(default_factory=list)  # the topics the writer proposed for the set's units, in order
    follow_on: Optional[Dict[str, Any]] = None                 # the next unit that depends on this one, done in the same turn: item, part, before, after, total
    method: str = ""                                           # how the increment was measured: rubric, evidence or read
    kept_artifacts: List[str] = field(default_factory=list)    # each record write this turn made or proposed besides the goal, said for the reader
    handed_off: Optional[Dict[str, Any]] = None                 # the deliverable's work handed to the deep harness: its goal, and whether it was queued

    def body(self) -> str:
        """The deliverable as kept on the goal: the overview, then each part under its separator."""
        out = (OVERVIEW_HEAD + self.overview.strip()) if self.overview.strip() else ""
        for n, part in enumerate(self.parts, 1):
            out += PART_SEPARATOR.format(n=n) + part.strip()
        if self.follow_on and self.follow_on.get("part"):
            out += FOLLOW_SEPARATOR.format(item=self.follow_on["item"]) + str(self.follow_on["part"]).strip()
        return out.strip()

    def increment_line(self) -> str:
        if self.before is None or self.after is None:
            return ""
        shown = lambda v: str(int(v)) if float(v).is_integer() else f"{v:g}"     # noqa: E731
        # Every count says what it counts (its basis), so a reader can trace it to what the code counted; the depth loop's own scores stay in the record.
        basis = {"rubric": "units in which a separate check found every rubric item, each with a quote from the unit",
                 "evidence": "questions answered with a quote found in the records, the turn's reads or the research",
                 "read": "the value its source reads"}.get(self.method, "measured by code")
        of = (f" of {self.total} {'asked' if self.method == 'evidence' else 'planned'}" if self.total else "")
        line = f"{self.measure_name}: {shown(self.before)} before, {shown(self.after)} after{of} (counted: {basis}; {self.as_of})."
        if self.follow_on and self.follow_on.get("part"):
            f = self.follow_on
            line += (f" Then, depending on it, {f['item']}: {shown(f['before'])} before, {shown(f['after'])} after"
                     + (f" of {f['total']} planned" if f.get("total") else "") + " (counted the same way).")
        return line

    def metric_line(self) -> str:
        if not self.metric_name:
            return ""
        if self.metric_after and self.metric_after == self.metric_before:
            return f"Milestone metric ({self.metric_name}), read again after the work: unchanged at {self.metric_after}, because it moves when a person does the step below."
        if self.metric_after:
            return f"Milestone metric ({self.metric_name}), read again after the work: was {self.metric_before or 'not recorded'}, now {self.metric_after}."
        return f"Milestone metric ({self.metric_name}): {self.metric_before or 'not recorded'}; it could not be read again just now."

    def reply_text(self) -> str:
        """The reply, composed by code: the deliverable first, then the measured increment, the milestone metric read again, then the one person-only step.
        Work handed to the deep harness as background work is acknowledged instead: what is being made, where it will be kept, and its before."""
        if self.status == "handed_off":
            shown = lambda v: str(int(v)) if float(v).is_integer() else f"{v:g}"     # noqa: E731
            lines = [f"I started {self.item[:1].lower() + self.item[1:] if self.item[:2] != self.item[:2].upper() else self.item} as background work: the deep agent "
                     f"builds it to its rubric and keeps it on the goal '{self.title}'."]
            if self.before is not None:
                lines.append(f"{self.measure_name}: {shown(self.before)} now" + (f" of {self.total} planned" if self.total else "")
                             + f" (counted: units in which a separate check found every rubric item; {self.as_of}); the count is taken again when the work is kept.")
            if self.kept_artifacts:
                lines.append("Also kept on the milestone's record this turn:\n" + "\n".join(f"- {a}" for a in self.kept_artifacts))
            if self.ask:
                lines.append(f"The one step only a person can do: {self.ask.rstrip('.')}.")
            return "\n\n".join(lines)
        item = self.item.rstrip(".")
        item = item[0].lower() + item[1:] if item[:2] != item[:2].upper() else item          # "Scripts for ..." reads "scripts for ..." mid-sentence; an acronym stays
        what = f"{item} (part {len(self.parts)}" + (f" of {self.total})" if self.total else ")") if self.parts else item
        lead = {"created": f"I wrote {what} and kept it on the goal '{self.title}'.",
                "updated": f"I wrote {what} and kept it on the goal '{self.title}', updated in place (no second goal).",
                "unchanged": f"The goal '{self.title}' already holds this work, so nothing new was kept."}.get(self.kept_state, f"I wrote {what}.")
        parts = [lead, self.body()]
        tail = " ".join(x for x in (self.increment_line(), self.metric_line()) if x)
        if tail:
            parts.append(tail)
        if self.kept_artifacts:
            parts.append("Also kept on the milestone's record this turn:\n" + "\n".join(f"- {a}" for a in self.kept_artifacts))
        if self.ask:
            parts.append(f"The one step only a person can do: {self.ask.rstrip('.')}.")
        if self.notes:
            parts.append(self.notes)
        return "\n\n".join(p for p in parts if p.strip())


def milestone_pass(made: Progress, cue: Any, request: str = "") -> Dict[str, Any]:
    """The per-solution finding for the milestone's own solution, shaped from a progress step already made (it never runs the work again): what the turn
    needs (its increment), the target it moves (the intermediate metric when one was defined, else the milestone's own metric, with its total), the risk a
    person-only step leaves, and the kept rubric as knowledge. ``request`` is the person's message, kept for the reader of the finding."""
    child = made.child_metric if isinstance(made.child_metric, Mapping) else {}
    metric_id = str(child.get("id") or (cue.metric_ids[0] if getattr(cue, "metric_ids", None) else ""))
    rubric = kept_rubric(getattr(cue, "row", {}) or {})
    return {"solution_id": getattr(cue, "rid", ""), "applies": True, "request": request,
            "needs": f"{made.item}: {made.increment_line()}".strip(": ") if made.status == "measured" else (made.why or "no AI increment could be made"),
            "target": {"metric_id": metric_id, "value": made.total, "done_when": getattr(cue, "done_when", "")},
            "risks": [f"Only a person can do: {made.ask}"] if made.ask else [], "conflicts": [],
            "knowledge": [rubric_entry(rubric.item, rubric.requirements, rubric.total, rubric.source, rubric.kind, rubric.done)] if rubric else [],
            "candidates": [], "before": made.before, "after": made.after}
