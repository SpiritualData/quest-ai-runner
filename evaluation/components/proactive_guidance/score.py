"""Score one proactive-guidance reply, two sided, and report it per class and per intent level.

Per case: ``ok``, ``class`` (nudge or restraint), ``level`` (quest, goal or rep), ``nudged`` (the reply points toward the long-term goal),
``awkward`` (a restraint case that got a pointer, or any pointer the grader finds forced, nagging or ahead of the answer), ``problems``
(checks by code) and ``note``.

Checks by code run first and a reply that fails one is not sent to the grader: a dash, a reply over the word limit, a figure in a pointing sentence that the case does
not hold (a nudge must be grounded, never invented), a nudge that does not name a real milestone and a real reading of the case, a nudge in the first
sentence ahead of the answer (unless the request asks what to do next), and, in a restraint case, any mention of the milestone or the reading.
Then one grader call (``claude_judge``): does it respond to what the person said, is a pointer present, at which level, is every fact in it from
the context, and is it awkward.

``aggregate`` reads the whole run: the two sided value is the mean of the nudge pass rate and the restraint pass rate, so a floor that never
nudges and a baseline that always nudges both score half or less."""
import re

import claude_judge
from proactive_guidance import arms

DASHES = ("—", "–", " -- ")
NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")
WORD = re.compile(r"[a-z0-9]+(?::[0-9]+)?")
FILLER = {"a", "an", "the", "by", "to", "of", "in", "on", "for", "and", "at", "is", "so"}
LEVELS = ("quest", "goal", "rep")


def tokens(text):
    return set(WORD.findall(text.lower()))


def numbers(text):
    return {n.strip(".,:") for n in NUMBER.findall(text)}


def names(reply_tokens, phrase):
    """The phrase is named when every content word of it is in the reply (so '25 km long run' is found in 'your 25 km long run')."""
    need = tokens(phrase) - FILLER
    return bool(need) and need <= reply_tokens


def reads(reply_numbers, value):
    """A reading is named when every number of it is in the reply ('2 of 3' needs a 2 and a 3)."""
    need = numbers(value)
    return bool(need) and need <= reply_numbers


def named(reply, case):
    toks, nums = tokens(reply), numbers(reply)
    return (any(names(toks, k) for k in case["milestone_keys"]), any(reads(nums, v) for v in case["metric_values"]))


def first_sentence(reply):
    return re.split(r"(?<=[.!?])\s", reply.strip(), maxsplit=1)[0]


def sentences(reply):
    return [part for part in re.split(r"(?<=[.!?])\s+", reply.strip()) if part]


def nudge_sentences(case, reply):
    """The sentences that point at the goal: those naming a milestone or a reading of the case, or speaking of a milestone or target.
    A figure in one of them must be the case's; a figure elsewhere in the reply (a fact about the world) is the grader's to judge."""
    found = []
    for sentence in sentences(reply):
        milestone, reading = named(sentence, case)
        if milestone or reading or re.search(r"\b(?:milestone|target)\b", sentence, re.I):
            found.append(sentence)
    return found


def problems_by_code(case, reply):
    found = []
    if not reply:
        return ["empty reply"]
    if any(d in reply for d in DASHES):
        found.append("dash")
    if len(reply.split()) > arms.MAX_WORDS:
        found.append(f"{len(reply.split())} words")
    held = " ".join([arms.context_text(case), *case.get("recent", []), case["message"], case["plain_answer"]])
    invented = sorted(n for sentence in nudge_sentences(case, reply) for n in numbers(sentence) - numbers(held))
    if invented:
        found.append("figure not in the case: " + ",".join(invented))
    milestone, reading = named(reply, case)
    if case["class"] == "nudge":
        if not milestone:
            found.append("names no real milestone")
        if not reading:
            found.append("names no real reading")
        if not case.get("answer_may_name_milestone"):
            if named(first_sentence(reply), case)[0]:
                found.append("nudge ahead of the answer")
    elif milestone or reading:
        found.append("nudge where none belongs")
    return found


def grader_prompt(case, reply):
    recent = "\n".join(case.get("recent", [])) or "(none)"
    return (
        "An AI assistant inside a goal tracking app holds the context below and has replied to the person. Grade only the reply.\n\n"
        f"CONTEXT THE ASSISTANT HOLDS\n{arms.context_text(case)}\n\nEARLIER IN THE CONVERSATION\n{recent}\n\n"
        f"THE PERSON SAID: {case['message']}\n\nTHE ASSISTANT REPLIED: {reply}\n\n"
        'Reply with one JSON object: {"responds": true or false, "pointer": true or false, "pointer_level": "none" or "goal" or "quest" or "rep", '
        '"grounded": true or false, "awkward": true or false, "reason": "one short sentence"}.\n'
        "responds: the reply answers or directly responds to what the person said, before anything else.\n"
        "pointer: the reply also nudges the person toward their long-term goal, a next milestone, or their next step on it, beyond what they asked "
        "(a plain answer that merely mentions the work they asked about is not a pointer).\n"
        "pointer_level: the level the pointer works at. goal = the specific goal or target in front of the person right now. quest = the quest's "
        "next milestone or outcome. rep = it picks among several quests the assistant looks after, or speaks of the wider intent. none = no pointer.\n"
        "grounded: every milestone, date, number and quest the reply names is in the context (false if it invents or distorts any).\n"
        "awkward: the pointer is forced, nagging, irrelevant to the moment, repeats something already said, ignores that the person is upset or "
        "pressed for time, or takes over the reply. False when there is no pointer and the reply is a fitting answer.")


def score_case(fn, case):
    got = fn(case)
    reply = (got.get("reply") or "").strip()
    row = dict(klass=case["class"], level=case["level"], kind=case["kind"], nudged=False, awkward=False, problems=[], got=reply[:160],
               **{"class": case["class"]})
    row["problems"] = problems_by_code(case, reply)
    if row["problems"]:
        row["nudged"] = case["class"] == "restraint" and any("where none belongs" in p for p in row["problems"])
        row["awkward"] = row["nudged"] or any("ahead of the answer" in p for p in row["problems"])
        return dict(row, ok=False, note="; ".join(row["problems"]))
    verdict = claude_judge.ask_json(got["provider"], grader_prompt(case, reply))
    if "error" in verdict:
        return dict(row, ok=False, note="grader: " + str(verdict["error"]))
    pointer, awkward = verdict.get("pointer") is True, verdict.get("awkward") is True
    row.update(nudged=pointer, awkward=awkward or (case["class"] == "restraint" and pointer))
    responds, grounded = verdict.get("responds") is True, verdict.get("grounded") is True
    if case["class"] == "nudge":
        right = pointer and verdict.get("pointer_level") == case["expected_level"]
        ok = responds and right and grounded and not awkward
    else:
        ok = responds and not pointer and not awkward
    return dict(row, ok=ok, note=("ok: " if ok else "") + str(verdict.get("reason", ""))[:150] + f" [pointer {verdict.get('pointer_level')}]")


def rate(rows):
    return (sum(1 for r in rows if r.get("ok")), len(rows))


def fraction(pair):
    return f"{pair[0]}/{pair[1]}"


def aggregate(rows):
    n = len(rows) or 1
    nudge = [r for r in rows if r.get("class") == "nudge"]
    restraint = [r for r in rows if r.get("class") == "restraint"]
    a, b = rate(nudge), rate(restraint)
    parts = [a[0] / a[1] if a[1] else 0.0, b[0] / b[1] if b[1] else 0.0]
    value = sum(parts) / 2
    levels = "; ".join(
        f"{lv} nudge {fraction(rate([r for r in nudge if r.get('level') == lv]))} restraint {fraction(rate([r for r in restraint if r.get('level') == lv]))}"
        for lv in LEVELS)
    awkward = sum(1 for r in restraint if r.get("awkward"))
    calls = sum(r.get("calls", 0) for r in rows)
    return (f"two-sided value {value:.2f} (nudge cases right {fraction(a)}, restraint cases right {fraction(b)}); awkward on restraint cases "
            f"{awkward}/{len(restraint)}; by level: {levels}; {calls / n:.2f} model calls per case, reply and grader")
