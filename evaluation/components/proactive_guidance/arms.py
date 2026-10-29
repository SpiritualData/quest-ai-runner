"""Shared machinery of the arms: the prompt around one chat turn, and the reply model on the subscription.

All arms see the same labelled context (the quest, its next milestone and its current metric reading, as the intent frame and the quest
facts put them in a quest-scoped prompt). The reply voice is the repository's own ``REPLY_VOICE_SYSTEM`` (which already tells the model to
answer first and be proactive only where it is worth the person's time); the candidate arm adds one directive about the long-term goal.
The reply model is the cheapest tier through ``claude -p`` (``claude-haiku``), so API dollars are zero."""
import json
from pathlib import Path

import common  # noqa: F401  (puts the repository on the path)
import claude_judge

HERE = Path(__file__).resolve().parent
REPLY_MODEL = "claude-haiku"
MAX_WORDS = 110          # what the prompt asks for
HARD_MAX_WORDS = 150     # what the scorer tolerates (an urgent how-to needs its steps)
BREVITY = (f"\n\nThis turn is text only and you cannot change anything, so never say you did something. Keep the reply under {MAX_WORDS} "
           "words. Use only the facts given in this prompt and never invent a quest, date, number or milestone.")
GUIDANCE_DIRECTIVE = (
    "\n\nLong-term guidance. The context above names where this work sits, its next milestone and its current metric reading, and nothing "
    "else is known about them. Answer what the person asked first and completely. Then, only when it is a natural moment (the request is "
    "about this work, or they are finishing, idle or asking what to do next) add at most one short sentence that points them toward the "
    "next milestone, at the level that fits the moment: the goal in front of them, the quest, or, if you look after several quests, the one "
    "whose milestone matters most now. Name the real milestone and the real current reading exactly as given. Leave the pointer out "
    "completely, with no encouragement about the long-term goal at all, when the matter is urgent or about safety, the person is upset, "
    "the question is a quick narrow or unrelated lookup, or the milestone was already raised in this conversation or they asked you to stop.")

GUIDANCE_DIRECTIVE_V2 = (
    "\n\nLong-term guidance. The context above holds the next milestone or target and its current reading; use only those.\n"
    "Step 1, decide. Write the answer alone, with no word about milestones, targets, goals, progress or what is due, when ANY of these holds: "
    "the matter is urgent, an injury, a safety issue, an outage or due today; the person is upset, discouraged or worried; the message is a "
    "lookup of a single fact (a time, place, name or number), a calculation, format, translation or recommendation that does not depend on this work; it has nothing to do with this work; the milestone was already raised "
    "in this conversation or they asked you to stop. When in doubt, write the answer alone, except that a person who says they are done, asks what else or what next, or is saying goodbye is always step 2, whatever they just worked on.\n"
    "Step 2, otherwise (this includes a person reporting progress on the work, asking what is next or where things stand, and a person who is finishing or idle). Answer fully first. Then end with exactly one sentence, never more, that names the next target and where it stands now, "
    "using the context's own words and numbers. Choose it by what the context holds: if a goal is in focus, name that goal's next target and its current reading, not the quest's far milestone; if only one quest is described, name its next milestone and its current reading; if you look after several quests, the one sentence must name the next milestone and current reading of two of them (three words each is enough for the second), never just one, claiming an order between them only when the dates in the context show it. If a goal is in focus, the sentence must carry that goal's target and its Metric reading. If the person is only saying thanks or goodbye, answer in a few words and still add the one sentence. If the person is upset or discouraged, offer support only and suggest no review, plan or next step on the work. Never invent a date or figure. Never put the sentence before the answer. Write number ranges with the word to, never with a dash.")


def load_cases():
    return json.loads((HERE / "cases.json").read_text(encoding="utf-8"))


def context_text(case):
    return "\n".join(case["context"])


def user_prompt(case):
    parts = ["CONTEXT\n" + context_text(case)]
    if case.get("recent"):
        parts.append("EARLIER IN THIS CONVERSATION\n" + "\n".join(case["recent"]))
    parts.append("The person says: " + case["message"])
    return "\n\n".join(parts)


def live_reply(meter, voice, directive=""):
    """A live arm: the reply model answers the prompt under ``voice`` (the repository's ``REPLY_VOICE_SYSTEM``), plus the directive when given."""
    provider = claude_judge.cli_provider(meter, REPLY_MODEL)

    def reply(case):
        text = provider.answer([{"role": "user", "content": user_prompt(case)}], model=REPLY_MODEL,
                               system=voice + BREVITY + directive) or ""
        return {"reply": text.strip(), "provider": provider}
    return reply


def scripted(meter, make):
    """A scripted arm: ``make(case)`` is the reply; the judge is the only model that may be asked."""
    provider = claude_judge.cli_provider(meter, "grader-only")

    def reply(case):
        return {"reply": make(case), "provider": provider}
    return reply
