"""Score one first beat against the contract of its prompt, the specificity floor and a grader's support check, and weigh it.

Per case: ``ok`` (the beat is what the case calls right), ``quiet`` (nothing was said), ``contract`` (breaks of the prompt's own
contract found by code), ``unspecific`` (names nothing from the message), ``unsupported`` (a claim the turn at that moment does
not back, found by the grader), ``beat_seconds`` (turn start to the beat) and ``late`` (after the 5 second join of the first beat).

The checks by code run first and a beat that fails one is not sent to the grader, so a beat that is empty, dashed, figured out of
thin air or generic never costs a grader call.

``aggregate`` reads the whole run: share right (value), time to the first beat (time) and model calls per case, the grader's
included (cost)."""
import re

import claude_judge
from narrate_a_turn import arms

MAX_WORDS = 25
MAX_SENTENCES = 2
DASHES = ("—", "–", " -- ")
MARKDOWN = re.compile(r"[*#`]|^\s*(?:[-•]|\d+[.)])\s", re.M)
FIRST_PERSON = re.compile(r"\b(?:i|i'll|i'm|i've|i'd|let me|let's)\b", re.I)
FIGURE = re.compile(r"\d[\d,.]*")


def contract_problems(beat, message, recent):
    """Breaks of the first-beat prompt found by code: length, sentences, dash, markdown, voice, a figure the turn does not hold."""
    problems = []
    if len(beat.split()) > MAX_WORDS:
        problems.append(f"{len(beat.split())} words")
    if len(re.findall(r"[.!?](?:\s|$)", beat)) > MAX_SENTENCES:
        problems.append("more than two sentences")
    if any(d in beat for d in DASHES):
        problems.append("dash")
    if MARKDOWN.search(beat):
        problems.append("markdown")
    if not FIRST_PERSON.search(beat):
        problems.append("not first person")
    held = f"{message} {recent}"
    invented = [f for f in FIGURE.findall(beat) if f.strip(".,") not in held]
    if invented:
        problems.append("figure not in the turn: " + ",".join(invented))
    return problems


def names_something(beat, names):
    low = beat.lower()
    return any(name.lower() in low for name in names)


def grader_prompt(case, beat):
    recent = case.get("recent") or "(none)"
    return (
        "An AI assistant has just received a message and, before it has read or done anything, says one line out loud.\n"
        f"The person's message: {case['message']}\n"
        f"The recent conversation: {recent}\n"
        f"The line the assistant said: {beat}\n\n"
        "At this moment the assistant has read nothing and done nothing: it knows only the message and the recent conversation. "
        "A line is SUPPORTED when every claim in it is about what the person said, or about what the assistant is about to do next. "
        "It is UNSUPPORTED when it states a finding, a name, a date, an amount or the state of the person's account that the message "
        "and conversation do not contain, or says something was already done or found.\n"
        'Reply with {"supported": true or false, "unsupported_claims": ["each claim that is not backed"]}.')


def judged(case, got, provider):
    """The result dict of one case: the beat, when it arrived, and what the checks and the grader found. Used by every arm."""
    return {"beat": got.get("beat"), "seconds": got.get("seconds"), "provider": provider}


def score_case(fn, case):
    got = fn(case)
    beat = (got.get("beat") or "").strip()
    row = dict(kind=case["kind"], beat_seconds=got.get("seconds"), late=bool(got.get("seconds") and got["seconds"] > arms.LATE_SECONDS),
               quiet=not beat, contract=[], unspecific=False, unsupported=[], got=beat[:90], want="specific beat" if case["kind"] == "specific" else "quiet or new beat")
    if not beat:
        quiet_ok = case["kind"] == "repeat"
        return dict(row, ok=quiet_ok, note="quiet, as labelled" if quiet_ok else "no beat for a message with something to name")
    row["contract"] = contract_problems(beat, case["message"], case.get("recent", ""))
    row["unspecific"] = not names_something(beat, case["names"])
    if row["contract"] or row["unspecific"]:
        note = "; ".join(row["contract"] + (["names nothing from the message"] if row["unspecific"] else []))
        return dict(row, ok=False, note=note)
    verdict = claude_judge.ask_json(got["provider"], grader_prompt(case, beat))
    if "error" in verdict:
        return dict(row, ok=False, note="grader: " + str(verdict["error"]))
    claims = [str(c)[:80] for c in (verdict.get("unsupported_claims") or [])]
    supported = verdict.get("supported") is True and not claims
    row["unsupported"] = claims
    return dict(row, ok=supported, note="ok" if supported else "unsupported: " + "; ".join(claims or ["grader said not supported"]))


def aggregate(rows):
    n = len(rows) or 1
    ok = sum(1 for r in rows if r.get("ok"))
    by_kind = {}
    for r in rows:
        k = by_kind.setdefault(r.get("kind", "?"), [0, 0])
        k[0] += 1
        k[1] += 1 if r.get("ok") else 0
    parts = ", ".join(f"{kind} {v[1]}/{v[0]}" for kind, v in sorted(by_kind.items()))
    times = [r["beat_seconds"] for r in rows if r.get("beat_seconds")]
    calls = sum(r.get("calls", 0) for r in rows)
    avg = f"{sum(times) / len(times):.1f}s avg, {max(times):.1f}s max" if times else "no beat arrived"
    return (f"value {ok}/{n} supported specific beats ({parts}); quiet {sum(1 for r in rows if r.get('quiet'))}; contract breaks "
            f"{sum(1 for r in rows if r.get('contract'))}; unspecific {sum(1 for r in rows if r.get('unspecific'))}; unsupported "
            f"{sum(1 for r in rows if r.get('unsupported'))}; time to first beat {avg}, later than {arms.LATE_SECONDS:.0f}s "
            f"{sum(1 for r in rows if r.get('late'))}; cost {calls / n:.2f} model calls per case, the grader's included")
