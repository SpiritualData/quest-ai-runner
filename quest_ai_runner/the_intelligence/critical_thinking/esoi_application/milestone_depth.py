"""The quality checks and prompts behind a written unit: the writer, evidence and depth prompts, the sentence-support check, rubric-echo removal, number and status-word marking, and the brief for the deep harness.

Role: everything that decides whether a sentence may stay in a deliverable (a fact needs a source quote, a number or status word must be held by a source, a rubric repeated back is removed) and the prompts the depth loop writes and combines with. The loop itself runs inside ``milestone_ladder.make_progress`` because it closes over the turn.
"""
from __future__ import annotations

import re

from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

from .milestone_completion import work_record_line
from .milestone_facts import GROUNDING_CHARS, NO_EM_DASH, clip_line, log, parse_json, squash


WRITE_PROMPT = """You write one deliverable for a team's milestone. It is used as it is, so write the thing itself, never a plan to write it.

The milestone, from its records:
{records}

What this turn read and was shown, and the research gathered for this work (each source named; the only source for any fact about the product, the team or
past work):
{grounding}

The work item: {item}
Each unit must meet: {requirements}
{kept}
Reply with JSON only: {{"units": [{{"n": <number>, "topic": "<what that unit covers>", "why": "<the reason, from the sources>", "source": "<the source it rests on>"}}],
"part": "<the text the requirements above are checked on, complete>"}}. List every unit of the set in "units" (as many as the set needs). The requirements are
checked on "part" alone, so "part" is the very thing they describe: when they speak of a whole set (a list, every entry, the full list), "part" is that whole
set written out, each entry with what the requirements ask of it; when they speak of one unit, "part" is the next unit in full. The part is used as it is: for a
video, the words to be spoken; for copy, the copy; for a list, the list; never notes or a plan about it.
Facts and proposals are different. A fact (what the product does, a figure, a date, a name, a result) must come from the sources above, said as they say it;
never invent one. A point about the product restates a passage of the sources in its words; an example or a speaker the sources do not give is left out. A proposal (which topics to cover, which option to recommend, how to compare candidates) is your own work: make it, concretely, from the
sources, and say why. When the work compares options or prepares a decision, end with an explicit recommendation of one option, labelled as a proposal,
with its reasoning, even when the evidence is thin (say how thin). Name an option only when a source names it, with that source; never a placeholder option
("Candidate B", "another niche"); when the sources name fewer than two, say so and name what would supply another. Write "to be decided" only for a fact that only a person or an outside system can supply,
never for a proposal you can make. No first
person plural unless the records say who speaks; no bracket or blank slot to fill. {no_em_dash}"""


EVIDENCE_PROMPT = """You gather the evidence a team's decision needs. Answer only from the records and the reads below; quote the exact words each answer rests on.

The milestone, from its records:
{records}

What this turn read and was shown:
{grounding}

The work item: {item}
Each answer must meet: {requirements}

List the questions this decision needs answered (at most {limit}), and answer each one the records or the reads can answer. Reply with JSON only:
{{"questions": [{{"question": "<one full sentence>", "answer": "<the answer, or empty when the records and reads do not give it>",
"source": "<the record or read it comes from, by name>", "quote": "<the exact words of that record or read the answer rests on, a few words, empty when unanswered>"}}]}}.
Invent no figure, name or date. {no_em_dash}"""


ANSWER_PROMPT = """Answer one question a team's decision needs, only from the records and the reads below; quote the exact words the answer rests on.

The question: {question}

The milestone's records:
{records}

What this turn read and the research gathered (each source named):
{grounding}

Reply with JSON only: {{"answer": "<the answer, or empty when the sources do not give it>", "source": "<the record or read it comes from, by name>",
"quote": "<the exact words of that source the answer rests on, a few words>"}}. Invent no figure, name or date. {no_em_dash}"""


PART_PROMPT = """Write the few sentences one unit of a team's deliverable still lacks, as content of the unit itself (the words used as they are: for a script, the
words spoken), to be combined into it. Never write about the unit, its requirements or this instruction.

The deliverable: {item}
The unit as it stands:
{base}

What the unit still lacks: {need}

The milestone's records and this turn's reads, the only source for any fact:
{records}
{grounding}

Use only facts above; a proposal (a topic, a recommendation) is yours to make from them, with its reason. Write "to be decided" only for a fact only a
person or an outside system can supply. Reply with JSON only: {{"part": "<the sentences>"}}. {no_em_dash}"""


COMBINE_PROMPT = """Write one unit of a team's deliverable as a single coherent text, used as it is (for a script, the words spoken, in order), from the unit as it
stood and the added pieces below. Keep what each piece adds; drop repeats, notes, instructions and anything about the unit itself.

The deliverable: {item}
The unit as it stood:
{base}

Added pieces:
{pieces}

The milestone's records and this turn's reads, the only source for any fact:
{records}
{grounding}

State a fact (what the product does, a figure, a link, a name) only as the sources say it. Reply with JSON only: {{"part": "<the whole unit>"}}. {no_em_dash}"""


SUPPORT_PROMPT = """Classify each numbered sentence of a team's deliverable.

The sources (records, what the turn read, research gathered):
{reference}

The sentences:
{sentences}

For each sentence, say its kind: "fact" (it states something about the product, a team, people, figures, dates, places or past results), "proposal" (it
proposes, recommends, compares or plans, and is worded as such), or "plain" (a greeting, a call to action, a transition, or "to be decided"). A sentence that
says what a product does, has, costs or offers, or where to find it (a feature, a price, a link, an offer, a figure), is a "fact" even inside a proposal, a
spoken line or a call to action; so is an example presented as real. For a fact,
quote the exact words of the sources it rests on (a few words, copied exactly), or leave the quote empty when no source states it. Reply with JSON only:
{{"sentences": [{{"n": <number>, "kind": "fact" | "proposal" | "plain", "quote": "<exact words from the sources, or empty>"}}]}}."""


SUPPORT_VIEWS = 2       # independent classifications; a sentence one of them finds to be an unsupported fact is taken out


def supported_sentences(text: str, reference: str, decide: Callable[[str], str], keep_line_starts: bool = False) -> Tuple[str, List[str]]:
    """``(text, removed)``: every sentence that states a fact must rest on words code finds in ``reference`` (the records, the turn's reads and the research);
    one that does not is taken out. Two independent classifications (the second sees the sentences in reverse order); a sentence either finds to be a fact
    with no quote in the sources is removed, so a missed invention needs both to miss it. Proposals and plain sentences stay. The structure, not the
    wording, decides: a quote is checked by code. With ``keep_line_starts`` the first sentence of each line (a proposed item's own name) is never taken out;
    the sentences that argue for it are checked like any other."""
    from hubofhubs.api import joined, units_of
    units = units_of(text)
    starts = {k for k, u in enumerate(units) if keep_line_starts and (k == 0 or "\n" in (u.lead or ""))}
    # A list number ("1.") is split off as its own sentence: the sentence after it is the item's name.
    starts |= {k + 1 for k in list(starts) if k + 1 < len(units) and re.fullmatch(r"\d+[.)]", units[k].text.strip())}
    if not units:
        return text, []
    source = squash(reference)
    order = list(range(len(units)))
    unsupported: set = set()
    for view in range(SUPPORT_VIEWS):
        seen = order if view % 2 == 0 else order[::-1]
        listing = "\n".join(f"{n}. {units[k].text}" for n, k in enumerate(seen, 1))
        data = parse_json(decide(SUPPORT_PROMPT.format(reference=str(reference)[:GROUNDING_CHARS * 2], sentences=listing)))
        for row in data.get("sentences") or []:
            try:
                k = seen[int(row.get("n")) - 1]
            except (TypeError, ValueError, IndexError, AttributeError):
                continue
            quote = squash(row.get("quote"))
            if row.get("kind") == "fact" and not (len(quote.split()) >= 3 and quote in source):
                unsupported.add(k)
    unsupported -= starts
    removed = [units[k].text for k in sorted(unsupported)]
    for k in unsupported:
        if sum(u.kept for u in units) > 1:
            units[k].kept = False
    # Every kept sentence, a proposal or an item's own name too, may carry a number or a status word only when the sources hold it (see ``marked_claims``).
    kept = [k for k, u in enumerate(units) if u.kept and not re.fullmatch(r"\d+[.)]", u.text.strip())]
    fixed = with_supported_markers([units[k].text for k in kept], reference, decide)
    for k, (text_now, ok) in zip(kept, fixed):
        if ok:
            units[k].text = text_now
        elif k in starts or sum(u.kept for u in units) <= 1:
            units[k].text = without_markers(units[k].text, reference)
        else:
            units[k].kept = False
            removed.append(units[k].text)
    # A sentence that leans on a removed one (a back-reference right after it) goes with it, so nothing kept rests on a fact that was taken out.
    from hubofhubs.api import cohere
    before = {k for k, u in enumerate(units) if u.kept}
    units, _ = cohere(units, "", None)
    removed += [units[k].text for k in sorted(before) if not units[k].kept]
    return joined(units), removed


#: Words that state a status (a decision made, a check done). A sentence may carry one only when the sources show it, in a sentence about the same thing.
STATUS_WORDS = re.compile(r"\b(?:agreed|confirmed|approved|verified|validated|signed off|finali[sz]ed|cross[- ]checked|checked against|double[- ]checked)\b", re.IGNORECASE)


ORDINAL_LIMIT = 10


LINK = re.compile(r"\b(?:https?://\S+|(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|ai|app|co|dev|me|tv|us|uk)\b(?:/\S*)?)", re.IGNORECASE)


NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


CONTENT_WORD = re.compile(r"[a-z]{5,}")


REPHRASE_PROMPT = """Each numbered sentence below states a number, a threshold or a status word ("agreed", "confirmed", "checked against ...") that the sources
do not hold. Rewrite each so it keeps its meaning without it: leave the number out, or say it is "to be set from <the named source that would give it>"; leave a
status word out, or say what is actually the case (a proposal is "proposed", not "agreed"). Never add a new number, name or date.

The sources:
{reference}

The sentences:
{sentences}

Reply with JSON only: {{"sentences": [{{"n": <number>, "text": "<the rewritten sentence>"}}]}}. {no_em_dash}"""


#: A sentence about the deliverable itself (its rubric, its requirements, what "this script" covers) is the scorer's language, never the deliverable's.
META = re.compile(r"\b(?:rubric|requirements?)\b|\bthis (?:script|unit|deliverable|draft|piece|section)\b|\b(?:the|each|every) (?:script|unit) (?:must|should|covers|names|is written)\b",
                  re.IGNORECASE)


ECHO_SHARE = 0.5        # a sentence whose content words are at least half one rubric line's (and three or more) repeats the rubric


def stems5(text: str) -> set:
    return {w[:5] for w in CONTENT_WORD.findall(str(text or "").lower())}


def without_rubric_echo(text: str, rubric: Sequence[str]) -> Tuple[str, List[str]]:
    """``(text, removed)``: the sentences that speak about the deliverable (``META``) or repeat a rubric line's words taken out by code, so the rubric and the
    scorer's wording never reach the kept text; the deliverable keeps only its own content."""
    from hubofhubs.api import joined, units_of
    units = units_of(text)
    lines = [stems5(r) for r in rubric if str(r).strip()]
    whole = [squash(r).rstrip(".") for r in rubric if len(str(r).split()) >= 2]
    removed = []
    for u in units:
        words = stems5(u.text)
        echo = (any(len(words & line) >= 3 and len(words & line) >= ECHO_SHARE * len(words) for line in lines)
                or any(w and w in squash(u.text) for w in whole))
        if (META.search(u.text) or echo) and sum(x.kept for x in units) > 1:
            u.kept = False
            removed.append(u.text)
    return joined(units), removed


def marked_claims(sentence: str, reference: str) -> List[str]:
    """The numbers and status words in ``sentence`` the sources do not hold, found by code: a number must appear in ``reference`` as a number of its own
    (thousands separators aside); a status word must appear in a sentence of ``reference`` that shares at least two content words with ``sentence``, so a
    "checked against X" claim needs a read of X that says so. [] when every one is held."""
    source = str(reference or "").replace(",", "")
    out = []
    for m in NUMBER.finditer(sentence):
        number = m.group(0).replace(",", "").rstrip(".")
        rate = re.match(r"\s*(?:%|percent\b|per cent\b)", sentence[m.end():], re.IGNORECASE)
        if number.isdigit() and int(number) <= ORDINAL_LIMIT and not rate:
            continue        # a small whole number counts or orders the deliverable's own pieces (unit 2, three points); a rate or a figure must be held
        if not re.search(r"(?<![\d.])" + re.escape(number) + r"(?![\d])", source):
            out.append(m.group(0))
    for m in LINK.finditer(sentence):
        if m.group(0).lower().rstrip("/.") not in str(reference or "").lower():
            out.append(m.group(0))
    words = set(CONTENT_WORD.findall(sentence.lower()))
    for m in STATUS_WORDS.finditer(sentence):
        status = m.group(0).lower().replace("-", " ")
        held = any(status in line.lower().replace("-", " ") and len(words & set(CONTENT_WORD.findall(line.lower())) - {status.split()[0]}) >= 2
                   for line in re.split(r"(?<=[.!?\n])\s+", str(reference or "")))
        if not held:
            out.append(m.group(0))
    return out


def with_supported_markers(sentences: Sequence[str], reference: str, decide: Optional[Callable[[str], str]]) -> List[Tuple[str, bool]]:
    """Each sentence as ``(text, ok)``: unchanged when ``marked_claims`` finds nothing; else rewritten once by ``decide`` without the unheld number or status
    word and checked again by code; ``ok`` is False when the rewrite still carries one (the caller drops the sentence, or strips the words from a name)."""
    out = [(s, not marked_claims(s, reference)) for s in sentences]
    bad = [k for k, (_, ok) in enumerate(out) if not ok]
    if not bad or decide is None:
        return out
    listing = "\n".join(f"{n}. {sentences[k]}" for n, k in enumerate(bad, 1))
    data = parse_json(decide(REPHRASE_PROMPT.format(reference=str(reference)[:GROUNDING_CHARS * 2], sentences=listing, no_em_dash=NO_EM_DASH)))
    for row in data.get("sentences") or []:
        try:
            k = bad[int(row.get("n")) - 1]
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
        text = " ".join(str(row.get("text") or "").split())
        if text and not marked_claims(text, reference):
            out[k] = (text, True)
    return out


def without_markers(text: str, reference: str) -> str:
    """``text`` with the status words and numbers the sources do not hold taken out by code (for a line that must stay, such as an item's name)."""
    for marker in marked_claims(text, reference):
        text = re.sub(r"\s*\b" + re.escape(marker) + r"\b", "", text, count=1)
    return " ".join(text.split())


STEP_PROMPT = """The one step only a person can do next, after the AI's work below:
{step}

What the AI kept (the deliverable) and the records:
{kept}

Rewrite the step so everything it points to (a list, copy, a link, a script, a proposal, a scheme) is something the kept deliverable or the records hold, called
by the name it has there; drop any pointer to something they do not hold. Keep who does it when the records say. One sentence. Reply with JSON only:
{{"step": "<the step>"}}. {no_em_dash}"""


POINTER = re.compile(r"\b(?:the|its|their) (?:proposed|drafted|suggested|new|tagged) ([a-z]+(?: [a-z]+)?)", re.IGNORECASE)


def held_source(label: Any, reference: str) -> str:
    """``label`` (a source a writer named) when the turn's reads hold it: most of its content words appear in ``reference``. "" for a source the turn never
    read, so no deliverable cites one."""
    label = " ".join(str(label or "").replace("(", " ").replace(")", " ").split())
    words = stems5(label)
    if not label or not words:
        return ""
    known = stems5(reference)
    held = len(words & known)
    return label if held >= min(2, len(words)) and held / len(words) >= SOURCE_SHARE else ""


SOURCE_SHARE = 0.75      # the share of a source label's content words the reads must hold for the label to stand


def kept_step(step: str, kept: str, reference: str, decide: Optional[Callable[[str], str]]) -> str:
    """The person-only step, pointing only at what the turn kept or the records hold: a step that points at a proposed or drafted thing the kept text does not
    name (found by code) is rewritten once from the kept text and checked again; then the line rule (``checked_line``) applies."""
    step = " ".join(str(step or "").split())
    if not step:
        return step
    known = squash(kept + " " + reference)
    def missing(text: str) -> List[str]:
        out = []
        for m in POINTER.finditer(text):
            words = [w[:5] for w in re.findall(r"[a-z]{4,}", m.group(1).lower())]
            if words and not any(w in known for w in words):
                out.append(m.group(1))
        return out
    if missing(step) and decide is not None:
        again = " ".join(str(parse_json(decide(STEP_PROMPT.format(step=step, kept=(kept + "\n\n" + reference)[:GROUNDING_CHARS], no_em_dash=NO_EM_DASH))).get("step") or "").split())
        step = again if again and not missing(again) else POINTER.sub(lambda m: m.group(0) if not missing(m.group(0)) else "the kept deliverable", step)
    return checked_line(step, kept + "\n" + reference, decide)


def checked_line(text: str, reference: str, decide: Optional[Callable[[str], str]]) -> str:
    """One line the reply or the kept deliverable shows (a title, an item's name, the person-only step, a note), held to the same rule as the deliverable's
    sentences: a number or a status word only when the sources hold it."""
    text = " ".join(str(text or "").split())
    if not text:
        return text
    (now, ok), = with_supported_markers([text], reference, decide)
    return now if ok else without_markers(text, reference)


EVIDENCE_QUESTIONS = 8


def solution_prompt(cue: Any, sid: str, task: str = "") -> str:
    """The hub's generated prompt for solution ``sid`` from the quest's own network of records (its records, metric and knowledge), "" when the quest's
    records do not hold it or the network cannot be built."""
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.solution_consideration import quest_network
    try:
        network = quest_network(str(cue.quest_id), cue.tables or {})
        if not network.has(sid):
            return ""
        return str((network.prompt(sid, task) or {}).get("text") or "")[:GROUNDING_CHARS]
    except Exception as error:  # noqa: BLE001  the milestone's records stand alone
        log.warning("milestone progress: the solution's prompt could not be built: %s: %s", type(error).__name__, error)
        return ""


DEEP_BRIEF = """Make this deliverable for a team's milestone, to completion quality, and keep it.

The work item: {item}
Its rubric (every unit must meet each item; a separate check counts the units that do): {rubric}
Already kept (continue after these; do not repeat them):
{have}

Where to keep it: the goal titled exactly "{title}" in this quest, its description starting with the line "Kept by Quest AI:" and then the deliverable,
updated in place (never a second goal).

{records}

What the turn read (each source named):
{grounding}

Build it out with depth: research what it needs (the product's documents and outside sources, each named), write each part, and check each against the
rubric before keeping it. A fact (what the product does, a figure, a link, a name) only as a source says it, with the source; a proposal labelled as one;
no placeholder and no blank slot. End your output with a line that says only "DELIVERABLE:", then the deliverable itself, exactly as kept. {no_em_dash}
{record}"""


def deliverable_brief(item: Mapping[str, Any], requirements: Sequence[str], have: Sequence[str], title: str, records: str, grounding: str,
                      record: Optional[Mapping[str, Any]] = None) -> Tuple[str, str]:
    """``(goal, brief)`` for the deep harness: the work item as the goal, and the brief from the deliverable's solution records, its rubric, what is kept
    and where it is kept. ``record`` (the quest, milestone, goal title, rubric and before) becomes the code-written work record line the task executor
    reads when the work finishes, to measure and keep it (``milestone_completion``)."""
    goal = f"Make {item.get('name')} to its rubric and keep it on the goal '{title}'"
    brief = DEEP_BRIEF.format(item=item.get("name"), rubric="; ".join(requirements) or "(none recorded)",
                              have="\n".join(f"- {clip_line(p)}" for p in have) or "(nothing yet)", title=title, records=records,
                              grounding=str(grounding or "")[:GROUNDING_CHARS], no_em_dash=NO_EM_DASH,
                              record=work_record_line(record) if record else "")
    return goal, brief
