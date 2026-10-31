"""Checks the code makes on a tool call BEFORE it runs, so a cheap planner cannot spend a write or a read on an id it made up.

Two mechanisms, both structural (no prompt words, no reading of the model's prose):

* ``check_quest_ids``: a quest id a call names must be one the turn has actually SEEN (the context the planner was shown, the
  user's words, the transcript, or a tool result such as ``find_quest``). An id the turn never saw is either mapped to the one
  seen quest it plainly means (the words in the invented id, such as ``q_riverside_10k``, appear in exactly one listed quest) or
  refused back to the planner with the valid ids. With no id of that shape anywhere in the evidence there is nothing to judge
  against, so the call goes through.
* ``companion_calls``: a read can declare ``read_companions`` (``list_quest_goals`` declares ``list_quest_notes``), the reads that
  hold the rest of what a person means by "the quest": figures, budgets and constraints live in notes, not in goals. The code
  runs them once per quest per turn, so an answer is never written from half a quest.

Generic: a quest id is any argument a tool fills from the turn's ``quest_id``; nothing here knows a deployment's id format
beyond its shape (a prefix and a separator, or a long hex string).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: Tokens that can be an identifier: word characters joined by ``_`` or ``-`` (or one long hex run).
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{2,}")
HEX_RE = re.compile(r"^[0-9a-fA-F]{16,}$")
WORD_RE = re.compile(r"[a-z0-9]+")
#: How many valid ids a refusal lists (the planner needs the choices, not the whole portfolio).
REFUSAL_LISTS_AT_MOST = 12
#: Words that say nothing about WHICH quest an id means.
FILLER_WORDS = frozenset({"the", "a", "an", "of", "my", "to", "and", "id", "quest", "q"})


def quest_id_args(spec: Any) -> List[str]:
    """The argument names ``spec`` fills from the turn's quest: what the model may only name from what it has seen."""
    return [arg for arg, key in (getattr(spec, "context_args", None) or {}).items() if key == "quest_id"]


def shape_of(token: str) -> str:
    """What kind of identifier ``token`` looks like: ``hex`` for a long hex run, else its leading letters and first separator
    (``q_`` for ``q_fit``), else just its leading letters. Two ids of one deployment share a shape."""
    if HEX_RE.match(token):
        return "hex"
    lead = re.match(r"[A-Za-z]+", token)
    head = lead.group(0).lower() if lead else ""
    rest = token[len(head):]
    return head + (rest[0] if rest[:1] in ("_", "-") else "")


def words_of_id(token: str, shape: str) -> List[str]:
    """The words an id spells, without its shape prefix (``q_riverside_10k`` -> riverside, 10k)."""
    if shape == "hex":
        return []
    body = token[len(shape):] if token.lower().startswith(shape) else token
    return [w for w in WORD_RE.findall(body.lower()) if w not in FILLER_WORDS]


def evidence_ids(evidence: str, shape: str) -> Dict[str, str]:
    """Every id-shaped token of ``shape`` in ``evidence`` -> the evidence line it appears on (its label)."""
    found: Dict[str, str] = {}
    for line in (evidence or "").splitlines():
        for token in TOKEN_RE.findall(line):
            if token not in found and shape_of(token) == shape and (shape == "hex" or "_" in token or "-" in token):
                found[token] = line.strip()
    return found


@dataclass
class IdVerdict:
    """What the guard decided about one call's quest id arguments."""
    args: Dict[str, Any]
    mapped: List[Tuple[str, str]] = field(default_factory=list)    # (id the model wrote, id the code used)
    refusal: Optional[str] = None


def resolve_unknown(token: str, candidates: Mapping[str, str]) -> Optional[str]:
    """The one candidate whose label holds the words ``token`` spells, if exactly one does best. Ties and no overlap give None."""
    words = words_of_id(token, shape_of(token))
    if not words:
        return None
    scored: List[Tuple[int, str]] = []
    for candidate, label in candidates.items():
        label_words = set(WORD_RE.findall(label.lower()))
        scored.append((sum(1 for w in words if w in label_words), candidate))
    scored.sort(reverse=True)
    best = scored[0] if scored else (0, "")
    runner_up = scored[1][0] if len(scored) > 1 else 0
    return best[1] if best[0] >= 1 and best[0] > runner_up else None


def refusal_text(tool: str, arg: str, token: str, candidates: Mapping[str, str]) -> str:
    listing = "\n".join(f"- {c}: {l.replace(c, '').strip(' :-.')[:100]}" for c, l in list(candidates.items())[:REFUSAL_LISTS_AT_MOST])
    return (f"{tool} NOT RUN: {arg} {token!r} is not an id this turn has seen. Use one of these ids, or call find_quest "
            f"with words from the quest's name:\n{listing}")


def check_quest_ids(spec: Any, args: Mapping[str, Any], evidence: str, pinned: Optional[str] = None) -> IdVerdict:
    """Check every quest id argument of a call against what the turn has seen. Mapped ids are replaced in the returned args."""
    out = dict(args or {})
    verdict = IdVerdict(args=out)
    for arg in quest_id_args(spec):
        token = str(out.get(arg) or "").strip()
        if not token or token == pinned or token in evidence:
            continue
        candidates = evidence_ids(evidence, shape_of(token))
        if not candidates:
            continue    # nothing of this shape was shown this turn: there is no basis to call the id made up
        chosen = resolve_unknown(token, candidates)
        if chosen:
            out[arg] = chosen
            verdict.mapped.append((token, chosen))
        else:
            verdict.refusal = refusal_text(getattr(spec, "name", "tool"), arg, token, candidates)
            break
    return verdict


def companion_calls(spec: Any, args: Mapping[str, Any], pinned: Optional[str], seen_reads: Iterable[Tuple[str, str]]
                    ) -> List[Dict[str, Any]]:
    """The reads ``spec`` declares as its companions, for the quest the call was about, minus those already read this turn.
    ``seen_reads`` holds (tool name, quest id) pairs already read."""
    names: Sequence[str] = getattr(spec, "read_companions", ()) or ()
    quest = str(args.get("quest_id") or pinned or "").strip()
    if not names or not quest or getattr(spec, "mutates", False):
        return []
    done = set(seen_reads)
    return [{"name": n, "args": {"quest_id": quest}} for n in names if (n, quest) not in done]


#: The receipt for a change the person did not order (they asked how to do it, or whether, and that is all).
ORDER_REFUSAL = ("TOOL {name} NOT RUN: the person asked a question and did not order this change. Answer it in words, with "
                 "the steps if they asked how, and change nothing.")
RECEIPT_QUEST_RE = re.compile(r'"quest_id":\s*"([^"]+)"')


def quests_in_receipt(text: str) -> List[str]:
    """The quest ids named in a tool receipt's ``(args {...})`` header (the receipt starts with it)."""
    return RECEIPT_QUEST_RE.findall(str(text)[:400])


LISTING_RE = re.compile(r"^\s*(?:[-*\u2022]\s*)?([A-Za-z0-9][A-Za-z0-9_\-]{2,}):\s+(\S.*)$")
#: Words that do not say which quest a message names (function words and the vocabulary every quest shares).
NAMING_STOPWORDS = frozenset((
    "the and for with that this what how much many from have are was but not you your they about into when which will can does did "
    "has had its was were been being than then them their there here just also only some any all out over under again more most "
    "left right still very per our own off its who whom why where while should would could may might must shall quest quests goal "
    "goals outcome state current status note notes mine".split()))


def listed_quests(evidence: str) -> Dict[str, str]:
    """The quests an evidence text lists as ``id: description`` lines (the shape of a context listing and of a ``find_quest``
    receipt) -> their description. An id needs a ``_`` or ``-`` or to be a long hex run, so a ``Note: ...`` line is not one."""
    found: Dict[str, str] = {}
    for line in (evidence or "").splitlines():
        hit = LISTING_RE.match(line)
        if hit and hit.group(1) not in found and (HEX_RE.match(hit.group(1)) or "_" in hit.group(1) or "-" in hit.group(1)):
            found[hit.group(1)] = hit.group(2)
    return found


def content_words(text: str) -> frozenset:
    return frozenset(w for w in WORD_RE.findall(str(text).lower()) if len(w) >= 3 and w not in NAMING_STOPWORDS)


def named_quests(message: str, evidence: str, minimum_shared: int = 2, at_most: int = 3) -> List[str]:
    """The listed quests the person's message names: those whose description shares at least ``minimum_shared`` content words
    with it, best first. Reads the USER's words against data the turn holds; it only decides what to READ, so a wrong guess costs
    one read and a missed one leaves the planner's own choice standing."""
    words = content_words(message)
    scored = []
    for quest, description in listed_quests(evidence).items():
        shared = len(words & content_words(description))
        if shared >= minimum_shared:
            scored.append((shared, quest))
    scored.sort(key=lambda pair: -pair[0])
    return [q for _, q in scored[:at_most]]


def detail_reads(registry: Any) -> List[str]:
    """The reads a registry's tools declare as companions: the ones that hold a quest's notes, figures and constraints."""
    names: List[str] = []
    for tool in registry.names():
        for companion in getattr(registry.get(tool), "read_companions", ()) or ():
            if companion not in names and registry.get(companion) is not None:
                names.append(companion)
    return names
