"""How an AI should WRITE each piece of a quest: one standard, built from the vendored YAML.

ONE STANDARD (2026-10-21). Every QUEST FIELD instruction (outcome, name, purpose, current_state,
preferences, acceptance_criteria, measurable_outcomes, scope, quest_goal, quest_completion_criteria,
strategies) and the overview of what a quest is come from ``quest_field_guidance.yaml`` in this
package: a byte-identical copy of the backend's ``app/prompts/quest_field_guidance.yaml``, the one
source of truth, grounded in ESOI. This module holds NO field text of its own. Keep the copy fresh
with ``scripts/sync_quest_field_guidance.py`` (``--check`` is the drift test's twin).

What stays here is role-specific: GOALS and HABITS are not quest fields, they are Implementations in
motion (see the overview), so their instructions, the goal period nuances and the one runner-specific
sentence on strategies are written here, and they refer to the canonical fields by name instead of
restating them.

Surfaces that consume it:

  * the field-specific direct tools in ``core/tools_quest_standard.py`` (each tool's description
    carries the overview once and the standard for the one field it writes);
  * any consumer's own generation path (``render_compact_block``);
  * the human review document (``render_markdown``).

``FieldInstruction`` keeps the parts separate so a surface can render as much as it can afford:
``what_it_is`` is the YAML ``esoi``, ``good_looks_like`` its ``summary``, plus ``rules``, ``good``,
``bad`` and ``does_not_belong``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

GUIDANCE_PATH = Path(__file__).with_name("quest_field_guidance.yaml")


def _load_guidance() -> Dict[str, Any]:
    with GUIDANCE_PATH.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


GUIDANCE: Dict[str, Any] = _load_guidance()


def what_a_quest_is(form: str = "short") -> str:
    """The overview of a quest in ESOI terms: ``short`` (for prompts and tool descriptions) or ``full``."""
    return str((GUIDANCE.get("what_a_quest_is") or {}).get(form) or "").strip()


@dataclass(frozen=True)
class FieldInstruction:
    """The writing standard for one quest field or one kind of quest item."""

    key: str
    label: str
    what_it_is: str
    good_looks_like: str
    rules: Tuple[str, ...] = ()
    good: Tuple[str, ...] = ()
    bad: Tuple[str, ...] = ()
    does_not_belong: Tuple[str, ...] = ()

    @property
    def weak_example(self) -> str:
        return self.bad[0] if self.bad else ""

    @property
    def strong_example(self) -> str:
        return self.good[0] if self.good else ""

    def render(self, *, examples: bool = True) -> str:
        lines = [f"{self.label.upper()}: {self.what_it_is}",
                 f"Good looks like: {self.good_looks_like}"]
        lines += [f"- {r}" for r in self.rules]
        if examples:
            lines += [f"Good: {g}" for g in self.good]
            lines += [f"Bad: {b}" for b in self.bad]
        lines += [f"Does not belong: {d}" for d in self.does_not_belong]
        return "\n".join(lines)


def _from_entry(key: str, entry: Dict[str, Any], *, suffix: str = "") -> FieldInstruction:
    esoi = str(entry.get("esoi") or "").strip()
    return FieldInstruction(
        key=key,
        label=str(entry.get("label") or key),
        what_it_is=(esoi + " " + suffix).strip() if suffix else esoi,
        good_looks_like=str(entry.get("summary") or "").strip(),
        rules=tuple(entry.get("rules") or ()),
        good=tuple(entry.get("good") or ()),
        bad=tuple(entry.get("bad") or ()),
        does_not_belong=tuple(entry.get("does_not_belong") or ()),
    )


#: Applies to every field, goal, strategy and habit. Rendered once ahead of any field block.
UNIVERSAL_RULES: Tuple[str, ...] = tuple(GUIDANCE.get("universal_rules") or ())

#: YAML field name -> instruction. ``fields`` first, then ``other_fields`` (scope); aliases excluded.
QUEST_FIELD_INSTRUCTIONS: Dict[str, FieldInstruction] = {
    name: _from_entry(name, entry)
    for section in ("fields", "other_fields")
    for name, entry in (GUIDANCE.get(section) or {}).items()
    if not entry.get("alias_of")
}

#: Alias name -> the field it stands for (``preferences_text`` -> ``preferences``).
QUEST_FIELD_ALIASES: Dict[str, str] = {
    name: str(entry["alias_of"])
    for name, entry in (GUIDANCE.get("fields") or {}).items() if entry.get("alias_of")
}


GOAL_INSTRUCTIONS = FieldInstruction(
    key="goal",
    label="Goal",
    what_it_is=("An Implementation in motion (see what a quest is): a time-boxed commitment "
                "scoped to one period (a day, week, month, quarter or year) that carries a chosen "
                "strategy toward the quest's outcome. A goal is not a quest field, and it is never "
                "the source for an acceptance criterion or for the outcome."),
    good_looks_like=("A short title that starts with a verb and names a concrete result, plus "
                     "completion criteria someone could check at the end of the period, sized to "
                     "that period."),
    rules=(
        "Title: verb first, a deliverable or result, at most about 12 words ('Publish 2 videos on "
        "the breathwork series', not 'Content').",
        "Give the goal its OWN measurable criterion (target value and unit when it has a number); "
        "never edit the quest's acceptance criteria, measurable outcomes or outcome to account "
        "for one goal.",
        "Link it to the acceptance criterion or measurable outcome it moves, when there is one.",
        "Size it to the period (see the period nuance below). A goal that cannot be finished in its "
        "period is split, not stretched.",
        "No duplicates: if an open goal with the same intent exists in that period, do not add "
        "another.",
    ),
    good=("Publish 2 breathwork videos and reply to every comment within 24 hours "
          "(criteria: 2 videos live by Sunday, 0 unanswered comments older than 24h).",),
    bad=("Work on marketing.",),
)


#: The period a goal is scoped to changes what a good goal looks like. Keys are the period kinds
#: Quest uses (day YYYY-MM-DD, week YYYY_W##, month YYYY_MM, quarter YYYY_Q#, year YYYY).
GOAL_PERIOD_NUANCES: Dict[str, str] = {
    "day": ("DAY goal: one concrete action finishable today, usually 15 minutes to 3 hours. Phrase "
            "it as the action itself ('Draft the intro section of the grant'), with a done state "
            "visible tonight. One to three day goals per quest per day; never a weekly result "
            "copied into a day. Give it a scheduled day and, when known, a time."),
    "week": ("WEEK goal: one deliverable or milestone finishable this week, broken into two to "
             "five day goals. Name the output ('Send the revised proposal to 3 funders'), not the "
             "theme. Two to four week goals per quest is plenty."),
    "month": ("MONTH goal: a measurable result for the month that several weekly goals add up "
              "to ('Grow to 350 subscribers', 'Ship the reminders feature to production'). It "
              "should name a number or a shipped thing and connect to a measurable outcome."),
    "quarter": ("QUARTER goal: a milestone that changes the quest's position: a capability built, "
                "a result reached, a stage finished ('Run a half marathon under 2:10'). One to "
                "three per quest, each traceable to a measurable outcome."),
    "year": ("YEAR goal: the direction-level result for the year, usually one per quest, a major stage "
             "toward the outcome, never a rewrite of it ('Finish my first marathon'). Avoid tasks "
             "and tactics here; they belong to shorter periods."),
}


#: The one sentence the runner adds to the canonical `strategies` entry.
STRATEGY_RUNNER_SUFFIX = ("Once the person accepts a strategy, goals and habits are generated from "
                          "it.")

STRATEGY_INSTRUCTIONS = _from_entry("strategy", GUIDANCE["fields"]["strategies"],
                                    suffix=STRATEGY_RUNNER_SUFFIX)


HABIT_INSTRUCTIONS = FieldInstruction(
    key="habit",
    label="Habit",
    what_it_is=("An Implementation in motion (see what a quest is): a recurring action tracked on a "
                "schedule, under one of the quest's strategies, whose entries roll up toward a "
                "goal. It is not a quest field."),
    good_looks_like=("A short verb-first name with the minimum dose in it, a frequency that the "
                     "person's preferences allow, and (when it matters) a time of day and "
                     "duration."),
    rules=(
        "Name the smallest version that still counts ('Run 20 min', 'Write 300 words'), so a bad "
        "day still produces a yes.",
        "Pick the frequency from what the person can sustain: daily only for small actions; "
        "otherwise named weekdays ('Mon, Wed, Fri').",
        "Attach it to the strategy it serves and, when there is one, the goal its entries count "
        "toward.",
        "Never add a habit that duplicates an existing one or conflicts with stated time limits.",
    ),
    good=("Run 30 min easy pace, Mon, Wed, Fri at 6:30am.",),
    bad=("Exercise regularly.",),
)


ITEM_INSTRUCTIONS: Dict[str, FieldInstruction] = {
    "goal": GOAL_INSTRUCTIONS,
    "strategy": STRATEGY_INSTRUCTIONS,
    "habit": HABIT_INSTRUCTIONS,
}


def period_kind(period: Optional[str]) -> Optional[str]:
    """``2026-10-11`` -> day, ``2026_W41`` -> week, ``2026_10`` -> month, ``2026_Q4`` -> quarter,
    ``2026`` -> year. A bare kind name is returned as-is; anything else is None."""
    p = str(period or "").strip()
    if not p:
        return None
    if p.lower() in GOAL_PERIOD_NUANCES:
        return p.lower()
    if len(p) == 10 and p[4] == "-" and p[7] == "-":
        return "day"
    if "_W" in p:
        return "week"
    if "_Q" in p:
        return "quarter"
    if len(p) == 7 and p[4] == "_":
        return "month"
    if len(p) == 4 and p.isdigit():
        return "year"
    return None


def instruction_for(key: str) -> Optional[FieldInstruction]:
    """The instruction for a quest field (``outcome`` ...) or item kind (``goal`` ...)."""
    return (QUEST_FIELD_INSTRUCTIONS.get(QUEST_FIELD_ALIASES.get(key, key))
            or ITEM_INSTRUCTIONS.get(key))


def goal_instructions_text(period: Optional[str] = None, *, examples: bool = True) -> str:
    """Goal instructions with the period nuance: just that period's when given, else all five."""
    kind = period_kind(period)
    nuances = ([GOAL_PERIOD_NUANCES[kind]] if kind
               else [GOAL_PERIOD_NUANCES[k] for k in GOAL_PERIOD_NUANCES])
    return GOAL_INSTRUCTIONS.render(examples=examples) + "\nBy period:\n" + "\n".join(
        f"- {n}" for n in nuances)


def field_instructions_text(key: str, *, examples: bool = True) -> str:
    """The full standard for one field or item, ready to put in a tool description or prompt."""
    if key == "goal":
        return goal_instructions_text(examples=examples)
    spec = instruction_for(key)
    return spec.render(examples=examples) if spec else ""


def render_compact_block(keys: Optional[Sequence[str]] = None) -> str:
    """A prompt block for a generation path that may write several of these at once.

    Opens with the short overview of what a quest is, then the universal rules, then each wanted
    field or item without examples (the examples stay in the per-field tools and the review
    document). By default every field and item, with the strategy item standing for ``strategies``.
    """
    wanted = (list(keys) if keys
              else [k for k in QUEST_FIELD_INSTRUCTIONS if k != "strategies"]
              + list(ITEM_INSTRUCTIONS))
    parts = ["WHAT A QUEST IS: " + what_a_quest_is("short"),
             "HOW TO WRITE QUEST FIELDS, GOALS, STRATEGIES AND HABITS (applies to every value you "
             "write into one of them):"]
    parts += [f"- {r}" for r in UNIVERSAL_RULES]
    for key in wanted:
        text = field_instructions_text(key, examples=False)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def render_markdown() -> str:
    """The whole standard as a review document: the full overview, then every field and item."""
    out: List[str] = [
        "# How Quest AI writes quest fields, goals, strategies and habits",
        "",
        "This is the exact text the AI is given when it writes each part of a quest. Each tool "
        "that changes one of these carries the overview and the matching section in its own "
        "description, and the in-app code path carries a compact form of the same rules. The "
        "quest field sections are rendered from the backend's quest field guidance file "
        "(vendored as `core/quest_field_guidance.yaml`; change them there, never here). Goals, "
        "strategy wording for the runner and habits are written in `core/quest_field_instructions.py`.",
        "",
        "## What a quest is",
        "",
        what_a_quest_is("full"),
        "",
        "## Rules for every field",
        "",
    ]
    out += [f"* {r}" for r in UNIVERSAL_RULES]
    out.append("")

    def section(spec: FieldInstruction, extra: Sequence[str] = ()) -> None:
        out.extend([f"## {spec.label}", "", f"**What it is.** {spec.what_it_is}", "",
                    f"**Good looks like.** {spec.good_looks_like}", ""])
        out.extend(f"* {r}" for r in spec.rules)
        out.append("")
        out.extend(extra)
        for label, items in (("Good", spec.good), ("Bad", spec.bad),
                             ("Does not belong", spec.does_not_belong)):
            out.extend(f"**{label}:** {item}" for item in items)
            if items:
                out.append("")

    for key, spec in QUEST_FIELD_INSTRUCTIONS.items():
        if key != "strategies":  # the strategy item below is built from the same entry
            section(spec)
    period_lines: List[str] = ["**By period.**", ""]
    period_lines += [f"* {v}" for v in GOAL_PERIOD_NUANCES.values()]
    period_lines.append("")
    section(GOAL_INSTRUCTIONS, period_lines)
    section(STRATEGY_INSTRUCTIONS)
    section(HABIT_INSTRUCTIONS)
    return "\n".join(out).rstrip() + "\n"
