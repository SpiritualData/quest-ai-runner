"""How an AI should WRITE each piece of a quest: one set of instructions per field, goal, strategy and habit.

WHY THIS EXISTS (2026-10-11). Quest fields written by AI read as generic and, in Joshua's word,
embarrassing: a vision statement that restates the category ("Improve my health"), a current state
that is a motivational paragraph instead of facts, measurable outcomes that cannot be measured,
goals that are the same sentence at every horizon. The cause was structural, not a weak model:
nothing anywhere told the model what a GOOD value for each field looks like, so every call site
improvised. This module is that missing standard, written once, and consumed by every surface that
can write these values:

  * the field-specific direct tools in ``core/tools.py`` (each tool's description IS the
    instruction for the one field it writes, so the model reads the standard at the moment it
    chooses to write that field, and only for that field);
  * any consumer's own generation path (quest-backend renders ``render_compact_block`` into its
    code-generation prompt next to its governed write helpers);
  * the human review document (``render_markdown``), so the owner reviews the exact text the model
    is given, not a paraphrase of it.

Keep it generic: nothing here names an organisation, a person or a deployment. It describes the
Quest data model, which is what QAR's standard Quest tools already target.

Editing: change the text HERE and every surface follows. ``FieldInstruction`` keeps the parts
separate (what the field is, what good looks like, rules, a weak and a strong example) so a
surface can render as much as it can afford.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class FieldInstruction:
    """The writing standard for one quest field or one kind of quest item."""

    key: str
    label: str
    what_it_is: str
    good_looks_like: str
    rules: Tuple[str, ...] = ()
    weak_example: str = ""
    strong_example: str = ""

    def render(self, *, examples: bool = True) -> str:
        lines = [f"{self.label.upper()}: {self.what_it_is}",
                 f"Good looks like: {self.good_looks_like}"]
        lines += [f"- {r}" for r in self.rules]
        if examples and self.weak_example:
            lines.append(f"Weak: {self.weak_example}")
        if examples and self.strong_example:
            lines.append(f"Strong: {self.strong_example}")
        return "\n".join(lines)


#: Applies to every field, goal, strategy and habit. Rendered once ahead of any field block.
UNIVERSAL_RULES: Tuple[str, ...] = (
    "Write in the person's own voice and words. Keep their specific nouns, numbers, names and "
    "dates; never swap them for a generic category word.",
    "Specific beats inspiring. No filler ('embark on a journey', 'unlock your potential', "
    "'holistic', 'leverage'), no exclamation marks, no emoji, no em dashes.",
    "Never invent a fact, number, date or constraint the person did not give and the record does "
    "not show. If a value needs a number you do not have, ask for it or leave it out.",
    "Merge, do not replace: read the current value first and keep everything in it that is still "
    "true. A new fact is added to the field, not swapped in for what was there.",
    "Only write a field the person asked to change. Anything else is a suggestion you offer, which "
    "the write path turns into an ask for their approval.",
)


QUEST_FIELD_INSTRUCTIONS: Dict[str, FieldInstruction] = {
    "outcome": FieldInstruction(
        key="outcome",
        label="Outcome (vision statement)",
        what_it_is=("The one sentence that names what the person is doing with this part of "
                    "their life, as they would say it. It is the quest's title and the thing "
                    "every goal serves."),
        good_looks_like=("One sentence, at most about 20 words, in first person or as a plain "
                         "statement of the end state, naming the concrete result and, when the "
                         "person gave one, the date or horizon."),
        rules=(
            "Describe the end state reached, not the activity ('I run a full marathon without "
            "walking', not 'Training for a marathon').",
            "Keep the one word the quest is really about. If the person's wording already does "
            "this, keep their wording exactly and change nothing.",
            "No metrics lists here: numbers that define success belong in measurable outcomes.",
            "Never rewrite it because of small work inside the quest (a task, a progress note, "
            "one new goal).",
        ),
        weak_example="Improve my fitness and health journey.",
        strong_example="Run the Berlin Marathon on 27 Sept 2026 without walking.",
    ),
    "purpose": FieldInstruction(
        key="purpose",
        label="Purpose (why it matters)",
        what_it_is="Why the person wants this outcome, in their own reasons.",
        good_looks_like=("Two or three sentences giving the real motive and what changes in their "
                         "life or work when the outcome is reached."),
        rules=(
            "Use only reasons the person stated or clearly implied; never supply a generic "
            "motive ('to live a healthier, happier life').",
            "Name who benefits when it is not only the person (a team, a family, users).",
        ),
        weak_example="To become the best version of myself.",
        strong_example=("My father had a heart attack at 55. I want proof to myself that I can "
                        "build the endurance he never had, and to keep up with my kids."),
    ),
    "current_state": FieldInstruction(
        key="current_state",
        label="Current state",
        what_it_is="Where the person stands today relative to the outcome: the honest baseline.",
        good_looks_like=("Short factual sentences or bullets: the measurable starting point, what "
                         "is already in place, the main obstacles, and resources or constraints. "
                         "Dated when a number may go stale."),
        rules=(
            "Facts, not feelings or advice. No plan or recommendations here.",
            "Lead with numbers the person gave ('runs 10 km in 58 min', '212 subscribers on 3 Oct').",
            "When updating, keep earlier facts that are still true and mark superseded numbers with "
            "their new value and date rather than deleting history silently.",
        ),
        weak_example="The user is motivated but faces some challenges with consistency.",
        strong_example=("As of 3 Oct 2026: longest run 14 km, 10 km in 58 min. Runs twice a week. "
                        "Left knee sore after runs over 12 km. Can train mornings before 7am only."),
    ),
    "preferences": FieldInstruction(
        key="preferences",
        label="Preferences",
        what_it_is=("How the person wants to work toward the outcome: methods they like or "
                    "refuse, values, and hard constraints such as time, budget, tools, people."),
        good_looks_like=("A short list of concrete do and do-not statements the AI can apply when "
                         "planning, each one something a plan could visibly respect or violate."),
        rules=(
            "Write constraints as checkable statements ('no more than 5 hours a week', 'no gym "
            "membership'), never vague tastes ('likes flexibility').",
            "Record only what the person said or chose; do not infer personality traits.",
        ),
        weak_example="Prefers a balanced approach that fits their lifestyle.",
        strong_example=("At most 4 runs a week, one long run on Sunday. Prefers outdoor running "
                        "over treadmill. No paid coaching. Wants the plan as a weekly checklist."),
    ),
    "acceptance_criteria": FieldInstruction(
        key="acceptance_criteria",
        label="Acceptance criteria",
        what_it_is=("The observable conditions under which an independent observer would agree "
                    "the outcome has been reached."),
        good_looks_like=("Two to five conditions, each observable or checkable by someone other "
                         "than the person, each with its evidence named."),
        rules=(
            "Each criterion is pass or fail, never 'better' or 'more'.",
            "Name the evidence (a race result, a published page, a signed contract, a count in a "
            "named system).",
            "Do not duplicate the measurable outcomes word for word; criteria say what counts as "
            "done, measurable outcomes track the numbers on the way.",
        ),
        weak_example="I feel confident and prepared.",
        strong_example=("Official race result shows a finish. GPS record shows no walking segment "
                        "longer than 30 seconds."),
    ),
    "measurable_outcomes": FieldInstruction(
        key="measurable_outcomes",
        label="Measurable outcomes",
        what_it_is=("The quest's checklist of numeric results that together show progress and "
                    "completion. The quest's headline progress is computed from them."),
        good_looks_like=("Each item: one metric, a target value, a unit, and (when known) a "
                         "baseline and a deadline. Two to five items per quest."),
        rules=(
            "Every item must be countable: 'Reach 1,000 YouTube subscribers', not 'Grow the "
            "channel'. Set target_value and target_unit whenever the text has a number.",
            "Prefer result metrics over effort metrics (subscribers, revenue, race time), with at "
            "most one effort metric.",
            "Add an item only when the person asked, or the quest is being specified for the first "
            "time. A new goal gets its own criteria instead of a new quest-level item.",
            "Never reword, merge or delete an existing item when adding one.",
        ),
        weak_example="Be more consistent with posting.",
        strong_example="Reach 1,000 YouTube subscribers (baseline 212 on 3 Oct 2026).",
    ),
    "scope": FieldInstruction(
        key="scope",
        label="Scope (routing)",
        what_it_is=("When an AI should pick THIS quest for a piece of work: the kinds of work, "
                    "names, projects and keywords that belong here, and what does not."),
        good_looks_like=("A few lines: in scope (work types, named people, products, repos, "
                         "places, keywords), then out of scope (nearby work that belongs to a "
                         "different quest)."),
        rules=(
            "Use the exact names that will appear in requests (product names, people, folders), "
            "since routing matches on them.",
            "Always include an out-of-scope line for the nearest sibling topic, so similar quests "
            "are not confused.",
            "Do not restate the outcome; scope is about routing work, not the goal.",
        ),
        weak_example="Anything related to this project.",
        strong_example=("In scope: the bakery's online orders, the web store, supplier emails "
                        "and invoices, the weekly price list. Out of scope: the catering side "
                        "business (its own quest), personal finances."),
    ),
    "quest_goal": FieldInstruction(
        key="quest_goal",
        label="Quest goal",
        what_it_is=("The overall goal statement the plan is built against: the outcome made "
                    "operational, with the horizon and the main lever."),
        good_looks_like=("One or two sentences: what will be achieved, by when, and by what main "
                         "means, consistent with the outcome and never contradicting it."),
        rules=(
            "Derive it from the outcome and current state; do not introduce a different goal.",
            "Include the deadline or horizon when known.",
        ),
        weak_example="Achieve success in running.",
        strong_example=("Finish the Berlin Marathon on 27 Sept 2026 by building to a 32 km long "
                        "run through a 20-week plan of four runs a week."),
    ),
    "quest_completion_criteria": FieldInstruction(
        key="quest_completion_criteria",
        label="Quest completion criteria",
        what_it_is=("What makes the QUEST itself complete (and so ready to be marked achieved), "
                    "as distinct from the outcome being nice to have."),
        good_looks_like=("One to three checkable conditions, usually the decisive acceptance "
                         "criterion plus any closing step (a review written, a handover done)."),
        rules=(
            "Must be checkable on a single day: a person could say yes or no.",
            "Consistent with acceptance criteria; never looser than them.",
        ),
        weak_example="When I feel done.",
        strong_example=("Race finished with an official result, and a one-page retrospective "
                        "written in the quest notes."),
    ),
}


GOAL_INSTRUCTIONS = FieldInstruction(
    key="goal",
    label="Goal",
    what_it_is=("A time-boxed commitment inside a quest's plan, scoped to one period (a day, "
                "week, month, quarter or year), that moves the quest toward its outcome."),
    good_looks_like=("A short title that starts with a verb and names a concrete result, plus "
                     "completion criteria someone could check at the end of the period, sized to "
                     "that period."),
    rules=(
        "Title: verb first, a deliverable or result, at most about 12 words ('Publish 2 videos on "
        "the breathwork series', not 'Content').",
        "Give the goal its OWN measurable criterion (target value and unit when it has a number); "
        "never edit the quest's measurable outcomes to account for one goal.",
        "Link it to the measurable outcome it moves, when there is one.",
        "Size it to the period (see the period nuance below). A goal that cannot be finished in its "
        "period is split, not stretched.",
        "No duplicates: if an open goal with the same intent exists in that period, do not add "
        "another.",
    ),
    weak_example="Work on marketing.",
    strong_example=("Publish 2 breathwork videos and reply to every comment within 24 hours "
                    "(criteria: 2 videos live by Sunday, 0 unanswered comments older than 24h)."),
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
    "year": ("YEAR goal: the direction-level result for the year, usually one per quest, close to "
             "the outcome itself or a major stage of it ('Finish my first marathon'). Avoid tasks "
             "and tactics here; they belong to shorter periods."),
}


STRATEGY_INSTRUCTIONS = FieldInstruction(
    key="strategy",
    label="Strategy",
    what_it_is=("An approach the person commits to for reaching the outcome: the HOW that goals "
                "and habits are generated from."),
    good_looks_like=("A short title naming the approach (3 to 8 words), and a description of two "
                     "or three sentences: what the approach is, why it should work for THIS "
                     "person (their current state and preferences), and what it implies doing "
                     "each week."),
    rules=(
        "An approach, not a goal or a single task ('Progressive long runs with 10% weekly "
        "increase', not 'Run 30 km').",
        "Respect the stated preferences and constraints; never propose what they ruled out.",
        "Ground the why in evidence or the person's own experience when available; do not claim "
        "studies you cannot name.",
        "Do not duplicate an existing strategy under new words.",
    ),
    weak_example="Stay motivated.",
    strong_example=("Progressive long run. Add about 10% to the Sunday run each week with a "
                    "lighter week every fourth week; it builds endurance without aggravating the "
                    "knee, and fits the one-long-run-a-week preference."),
)


HABIT_INSTRUCTIONS = FieldInstruction(
    key="habit",
    label="Habit",
    what_it_is=("A recurring action tracked on a schedule, under one of the quest's strategies, "
                "whose entries roll up toward a goal."),
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
    weak_example="Exercise regularly.",
    strong_example="Run 30 min easy pace, Mon, Wed, Fri at 6:30am.",
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
    return QUEST_FIELD_INSTRUCTIONS.get(key) or ITEM_INSTRUCTIONS.get(key)


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

    Compact on purpose (no examples): it rides along a code-generation prompt, so it states the
    standard and leaves the examples to the per-field tools and the review document.
    """
    wanted = list(keys) if keys else (list(QUEST_FIELD_INSTRUCTIONS) + list(ITEM_INSTRUCTIONS))
    parts = ["HOW TO WRITE QUEST FIELDS, GOALS, STRATEGIES AND HABITS (applies to every value you "
             "write into one of them):"]
    parts += [f"- {r}" for r in UNIVERSAL_RULES]
    for key in wanted:
        text = field_instructions_text(key, examples=False)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def render_markdown() -> str:
    """The whole standard as a review document, in the order a person reads a quest."""
    out: List[str] = [
        "# How Quest AI writes quest fields, goals, strategies and habits",
        "",
        "This is the exact text the AI is given when it writes each part of a quest. Each tool "
        "that changes one of these carries the matching section in its own description, and the "
        "in-app code path carries a compact form of the same rules. Edit any section to change "
        "how the AI writes that field.",
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
        if spec.weak_example:
            out.extend([f"**Weak:** {spec.weak_example}", ""])
        if spec.strong_example:
            out.extend([f"**Strong:** {spec.strong_example}", ""])

    for spec in QUEST_FIELD_INSTRUCTIONS.values():
        section(spec)
    period_lines: List[str] = ["**By period.**", ""]
    period_lines += [f"* {v}" for v in GOAL_PERIOD_NUANCES.values()]
    period_lines.append("")
    section(GOAL_INSTRUCTIONS, period_lines)
    section(STRATEGY_INSTRUCTIONS)
    section(HABIT_INSTRUCTIONS)
    return "\n".join(out).rstrip() + "\n"
