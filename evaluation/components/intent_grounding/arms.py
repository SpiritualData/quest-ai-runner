"""Shared machinery of the arms: the prompt around one chat turn, the grounding the judge is given, and the chat_quality
verdict. An arm decides only whether the intent frame block is in the prompt.

The reply model sees the quest's facts and the person's message. With the frame it also sees the block the shipped autopilot
brief puts in front of its model (``INTENT_FRAME_PREAMBLE`` and then the frame text), so the words that tell the model how to
use the frame are the repository's, not this file's. The judge is ``chat_quality``'s own (``judge.judge``: hard checks by
code first, then ``claude -p`` on the subscription), given the same grounding for every arm."""
import json
import sys
from pathlib import Path

import common  # noqa: F401  (puts the repository on the path)

HERE = Path(__file__).resolve().parent
CHAT = HERE.parent / "chat_quality"
sys.path.insert(0, str(CHAT))
import judge as chat_judge  # noqa: E402
import world_spec as WORLD  # noqa: E402

from quest_ai_runner.runner.autopilot_briefs import INTENT_FRAME_PREAMBLE  # noqa: E402,F401

chat_judge.OTHER_ACCOUNT_DATA.update(built=True, section="")   # the judge is never sent to a dev Quest from here

REPLY_MODEL = "claude-haiku"   # the family alias the claude CLI resolves; the subscription's cheapest tier
SYSTEM = (
    "You are Quest AI, the assistant inside a goal tracking app, answering one message from the person who owns the quest "
    "described below. Reply to them directly in plain text, in at most 150 words. You cannot change anything in this turn, so "
    "never say you saved, added, scheduled or changed something; offer to do it instead. Use only the facts given here and never "
    "invent a quest, goal, ideal, metric, solution or record. Never use em dashes or en dashes; use a comma, a colon or two "
    "sentences instead.")
NO_WRITES_NOTE = ("This turn is text only: no helper can run and nothing can be written, so a reply that says it changed "
                  "something is a false claim.")


def combined_pivots(case):
    """chat_quality's world pivots plus the ones the case defines for itself."""
    return {**WORLD.PIVOTS, **(case.get("pivot_defs") or {})}


def quest_facts(case):
    """The quest as the assistant sees it: a world quest's whole record (chat_quality's seeded world), or the outcome alone."""
    if case.get("quest"):
        return "\n".join(f"Quest {key.replace('_', ' ')}: {value}" for key, value in case["quest"].items())
    quest = WORLD.QUESTS[case["world_quest"]]
    lines = [f"Quest outcome: {quest['outcome']}", f"Acceptance criteria: {quest['acceptance_criteria']}",
             f"Current state: {quest['current_state']}", f"Timeline: {quest['timeline_days']} days", "Measurable outcomes:"]
    lines += [f"- {item}" for item in quest["measurable_outcomes"]]
    lines.append("Goals:")
    lines += [f"- {name} ({period}, {kind}): {criteria}" for name, period, kind, criteria in quest["goals"]]
    lines.append("Notes:")
    lines += [f"- {text}" for text, _ in quest["notes"]]
    return "\n".join(lines)


def records_block(case):
    """The seeded records a pivot names, as the assistant could read them: the pivot's own description and exact values."""
    names = list(case.get("data_pivots") or [])
    if not names:
        return ""
    pivots = combined_pivots(case)
    lines = ["Records you can read:"]
    for name in names:
        lines.append(f"- {pivots[name]['description']} Exact values: {json.dumps(pivots[name]['exact_values'])}")
    return "\n".join(lines)


def user_prompt(case, with_intent, preamble=None):
    parts = [quest_facts(case)]
    if with_intent and case.get("frame"):
        parts.append((preamble or INTENT_FRAME_PREAMBLE) + "\n" + case["frame"].strip())
    records = records_block(case)
    if records:
        parts.append(records)
    if case.get("capabilities"):
        parts.append(chat_judge_capabilities())
    parts.append(f"The person says: {case['message']}")
    return "\n\n".join(parts)


def chat_judge_capabilities():
    import world
    return world.CHAT_CAPABILITIES.strip()


def ground_truth(case):
    """What the judge is told is true, the same for every arm: the quest, the intent frame when the case has one, the
    capabilities when the case asks about them, and that the turn is text only."""
    parts = [quest_facts(case)]
    if case.get("frame"):
        parts.append("INTENT FRAME the server computed for this quest (by code from stored records, no model call). The quest "
                     "is placed under this intent. These ideals, metrics, solutions and gaps are real:\n" + case["frame"].strip())
    else:
        parts.append("This quest has no intent above it, so there is no intent frame.")
    records = records_block(case)
    if records:
        parts.append(records)
    if case.get("capabilities"):
        parts.append(chat_judge_capabilities())
    parts.append(NO_WRITES_NOTE)
    return "\n\n".join(parts)


def evidence_of(reply):
    return {"reply": reply, "errors": [], "kind": "inline", "actions": [], "delegated": [], "tools": [], "web_read": None,
            "code_text": ""}


def grade(case, reply, judge_provider):
    """The chat_quality verdict for a reply: the hard checks by code, then the judge unless a hard check already failed."""
    evidence = evidence_of(reply)
    pre = chat_judge.precheck(case, evidence, None, None, [])
    judged = None
    if not pre["hard_failures"]:
        judged = chat_judge.judge(case, evidence, [], pre, ground_truth(case), combined_pivots(case), provider=judge_provider)
        if judged.get("error"):
            return {"passed": False, "score": None, "precheck": pre, "judged": judged, "error": judged["error"], "reply": reply}
    return {"passed": chat_judge.case_passed(case, pre, judged), "score": chat_judge.final_score(pre, judged), "precheck": pre,
            "judged": judged or {"summary": "not judged: a hard check failed"}, "error": None, "reply": reply}


def validate(case):
    """The case schema: what chat_quality's loader refuses, plus this component's fields."""
    import runner as chat_runner
    problems = []
    for key in ("id", "kind", "area", "rubric", "message", "frame", "source"):
        if key not in case or case[key] in (None, [], ""):
            if not (key == "frame" and case.get("kind") == "control" and key in case):
                problems.append(f"missing {key}")
    if case.get("kind") not in ("intent", "control"):
        problems.append("kind must be intent or control")
    if case.get("kind") == "intent" and not case.get("frame"):
        problems.append("an intent case carries a frame")
    if case.get("kind") == "control" and case.get("frame"):
        problems.append("a control carries no frame")
    if not (case.get("quest") or case.get("world_quest")):
        problems.append("needs quest or world_quest")
    for item in case.get("rubric") or []:
        if chat_runner.OPTIONAL_RUBRIC.search(item):
            problems.append(f"optional rubric item {item[:50]!r}")
    pivots = combined_pivots(case)
    for key in ("must_use_pivots", "bonus_pivots", "data_pivots"):
        for name in case.get(key) or []:
            if name not in pivots:
                problems.append(f"unknown pivot {name} in {key}")
    for key in case.get("precheck") or {}:
        if key not in chat_runner.PRECHECK_KEYS:
            problems.append(f"unknown precheck key {key!r}")
    if case.get("dataset") not in chat_runner.DATASETS:
        problems.append("bad dataset")
    if problems:
        raise ValueError(f"{case.get('id')}: " + "; ".join(problems))


def judge_provider(meter):
    """The chat_quality judge's provider (``claude -p``), counted on the meter so the run's calls include the judge's."""
    return meter.wrap(chat_judge.claude_provider())


def reply_provider(meter):
    from quest_ai_runner.adapters.claude_cli_provider import ClaudeCliProvider
    return meter.wrap(ClaudeCliProvider(claude_path=chat_judge.DEV_ENV.get("QAR_CLAUDE_PATH") or "claude", timeout_seconds=420))


def live_arm(meter, with_intent, preamble=None):
    """A live arm: the reply model answers the prompt, the chat_quality judge grades the reply."""
    meter.model = REPLY_MODEL
    replier, judge = reply_provider(meter), judge_provider(meter)

    def answer(case):
        reply = replier.answer([{"role": "user", "content": user_prompt(case, with_intent, preamble)}], model=REPLY_MODEL, system=SYSTEM) or ""
        return grade(case, reply.strip(), judge)
    return answer
