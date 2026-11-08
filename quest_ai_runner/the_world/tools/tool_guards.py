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


#: The names of the retrieval adapter's operations (``list_operations``): the planner sees them in its catalog next to quest ids.
RETRIEVAL_OPERATION_NAMES = frozenset(("get_goal_context", "query_quest", "discover_goals", "get_reflection_context", "get_task"))


def check_quest_ids(spec: Any, args: Mapping[str, Any], evidence: str, pinned: Optional[str] = None,
                    not_quest_ids: Iterable[str] = ()) -> IdVerdict:
    """Check every quest id argument of a call against what the turn has seen. Mapped ids are replaced in the returned args.
    ``not_quest_ids`` are names that are never a quest (tool and operation names): they sit in the catalog the turn saw, so
    "seen in the evidence" alone would pass them."""
    out = dict(args or {})
    verdict = IdVerdict(args=out)
    names = set(not_quest_ids) | RETRIEVAL_OPERATION_NAMES
    for arg in quest_id_args(spec):
        token = str(out.get(arg) or "").strip()
        if token and token in names and token != pinned:
            candidates = {t: l for t, l in listed_quests(evidence).items()}
            verdict.refusal = (f"{getattr(spec, 'name', 'tool')} NOT RUN: {arg} {token!r} is the name of a tool or operation, not a "
                               f"quest. Give a quest id" + (":\n" + "\n".join(f"- {c}: {l[:100]}" for c, l in list(candidates.items())[:REFUSAL_LISTS_AT_MOST])
                                                            if candidates else ", or call find_quest with words from its name."))
            break
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


def reads_owed(spec: Any, gathered: Iterable[Mapping[str, Any]], registry: Any) -> List[str]:
    """The reads ``spec`` (a write) declares it needs first (``reads_first``) that have not run successfully this turn and that the registry offers."""
    names: Sequence[str] = getattr(spec, "reads_first", ()) or ()
    if not names or not getattr(spec, "mutates", False):
        return []
    done = {str(e.get("locator") or "")[5:] for e in gathered
            if str(e.get("locator") or "").startswith("tool:") and e.get("tool_status") in ("applied", "noop")}
    return [n for n in names if n not in done and registry is not None and registry.get(n) is not None]


#: The receipt for a write whose required read had not run yet this turn.
READ_FIRST_NOTE = ("TOOL {name} NOT RUN YET: it needs {reads} read first this turn, and that read has now run (its result is above). Decide again from "
                   "what it shows: when what the person asked for is already there, make nothing and say it is already there; otherwise call {name} again.")


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


#: Two writes of one tool on one quest are the same item when their words overlap at least this much (Jaccard on the lower-cased
#: word sets). 0.75 is conservative: a note saved twice with a rewording ("Physio cleared easy cycling from Monday; no running" vs
#: "The physio cleared easy cycling from Monday, no running") scores 0.8 to 0.9, while two goals that share a template ("Run three
#: times this week" vs "Swim three times this week": 4 of 6 words) or a short phrase ("Plumbing visit" vs "Tile the walls": 0) stay apart.
NEAR_DUPLICATE_OVERLAP = 0.75
#: A text under this many words is compared for equality only: one word of overlap on a title of two words proves nothing.
NEAR_DUPLICATE_MIN_WORDS = 4


def text_words(text: str) -> frozenset:
    return frozenset(WORD_RE.findall(str(text).lower()))


def same_item(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    """Whether two argument sets name the same item: the same keys, every non-text value equal, every text value equal or (when long
    enough to judge) overlapping nearly entirely with the same numbers in both. A differing figure ("5k" against "10k") is a
    different item however much else the words share."""
    if set(first) != set(second):
        return False
    wording = False
    for key, left in first.items():
        right = second[key]
        if left == right:
            continue
        if not (isinstance(left, str) and isinstance(right, str)) or key.endswith("id"):
            return False
        a, b = text_words(left), text_words(right)
        if min(len(a), len(b)) < NEAR_DUPLICATE_MIN_WORDS or any(w.isdigit() or any(c.isdigit() for c in w) for w in a ^ b):
            return False
        if len(a & b) / len(a | b) < NEAR_DUPLICATE_OVERLAP:
            return False
        wording = True
    return wording


def merge_near_duplicates(calls: Sequence[Mapping[str, Any]], is_write: Any, pinned: Optional[str] = None
                          ) -> Tuple[List[Mapping[str, Any]], List[Tuple[str, Mapping[str, Any]]]]:
    """Of the calls one step proposes, drop each write that repeats an earlier write of the same tool on the same quest with the
    same item in other words (the cheap planner's habit of saving a note or a goal twice). ``is_write(name)`` says whether a tool
    changes data; reads are never merged. Returns the calls to run and the dropped ones as (name, args). The first wording wins."""
    kept: List[Mapping[str, Any]] = []
    dropped: List[Tuple[str, Mapping[str, Any]]] = []
    for call in calls:
        name = call.get("name") or ""
        args = dict(call.get("args") or {})
        target = str(args.get("quest_id") or pinned or "")
        twin = None
        if is_write(name):
            twin = next((k for k in kept if (k.get("name") or "") == name
                         and str((k.get("args") or {}).get("quest_id") or pinned or "") == target
                         and same_item(dict(k.get("args") or {}), args)), None)
        if twin is None:
            kept.append(call)
        else:
            dropped.append((name, args))
    return kept, dropped


#: The most characters of the person's message used as the web query a guard issues for them.
WEB_QUERY_CHARS = 200


def web_was_read(gathered: Iterable[Mapping[str, Any]]) -> bool:
    """Whether this turn already ran a web search or fetched a web page (a failed one counts: it was tried)."""
    for entry in gathered:
        if str(entry.get("rel_path") or "").startswith("web_search:") or str(entry.get("locator") or "").startswith("web extract"):
            return True
    return False


def web_read_owed(verdict: Optional[Mapping[str, Any]], web_configured: bool, gathered: Iterable[Mapping[str, Any]],
                  message: str) -> Optional[Dict[str, Any]]:
    """The web read the code runs before an answer, or None. Owed when the reach judge settled that what the person asks lives in
    the world right now (``reach == "world"``: a current price, a latest version), web research is configured, and the planner
    chose to answer without a single web read. A structured verdict, never the answer's words."""
    if not verdict or verdict.get("reach") != "world" or not web_configured or not str(message).strip():
        return None
    if web_was_read(gathered):
        return None
    return {"web": str(message).strip()[:WEB_QUERY_CHARS]}


def drop_web_reads_for_own_records(verdict: Optional[Mapping[str, Any]], reads: Sequence[Mapping[str, Any]]
                                   ) -> Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]:
    """Split a step's reads into (kept, dropped web reads). Once the reach judge has settled that what the person asks lives in
    their own records (``reach == "inside"``), a web search or page fetch is not reachable work: a small planner reads a date or a
    figure the person's own quest already states as if it were a public fact and searches the web for it, then answers from a wrong
    page. The structured verdict decides, never the words of the planner's query. No verdict keeps every read."""
    if not verdict or verdict.get("reach") != "inside":
        return list(reads), []
    kept = [r for r in reads if not (isinstance(r, Mapping) and (r.get("web") is not None or r.get("web_page")))]
    return kept, [r for r in reads if r not in kept]


def dated_file_reads_owed(message: str, retrieval: Any, today: Optional[Any] = None) -> List[Dict[str, Any]]:
    """The file finds the code runs before an answer or a question back to the person, or []. Owed when the person's OWN message points
    at a time ("yesterday", "last thursday", "this week") and a remote file source is wired: files named by date are found by the
    date the person named, one find per source, so the answer is not "I don't have it" for a file that is there. Reads the person's
    words, never the planner's."""
    from datetime import date as calendar_date
    from quest_ai_runner.the_world.tools.file_reading.date_references import date_references_in
    from quest_ai_runner.the_world.tools.file_reading.remote_files_adapter import RemoteFilesAdapter

    day = today or calendar_date.today()
    references = date_references_in(message, day)
    if not references or retrieval is None:
        return []
    members = getattr(retrieval, "adapters", None) or [retrieval]
    aliases = [a.alias for a in members if isinstance(a, RemoteFilesAdapter)]
    phrase = references[0][0]
    return [{"query": {"files": alias, "op": "find", "date": phrase, "max_results": 40}} for alias in aliases[:3]]


#: Capitalised words that are not a person's name: weekdays, months and the product's own words.
NOT_A_NAME = frozenset(("monday tuesday wednesday thursday friday saturday sunday january february march april may june july august "
                        "september october november december quest claude chat ai").split())
CAPITALISED_WORD_RE = re.compile(r"\b[A-Z][a-z]{2,}\b")
#: The most names one message turns into reads, and how far back an undated search looks.
PAST_ACTIVITY_MAX_NAMES = 2
PAST_ACTIVITY_DAYS = 30


def names_in(message: str) -> List[str]:
    """The capitalised words of a person's message that could be a person's name: not the first word of a sentence, not a weekday,
    month or product word. The person's OWN words, never a reply's; a wrong guess costs one read that finds nothing."""
    names: List[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", str(message or "")):
        words = sentence.strip().split(" ", 1)
        rest = words[1] if len(words) > 1 else ""
        for word in CAPITALISED_WORD_RE.findall(rest):
            if word.lower() not in NOT_A_NAME and word not in names:
                names.append(word)
    return names


def past_activity_reads_owed(message: str, action: str, operation: Optional[str], today: Optional[Any] = None) -> List[Dict[str, Any]]:
    """The reads of what people asked the AI and how it answered, which the code runs before the planner answers or asks the person for
    a title or a date, or [].  Owed when the consumer names the operation (``operation``), the person's own message names someone, and
    the planner is about to ask a question back (``clarify``) or the message points at a time ("last thursday", "this week"). The
    operation searches only what the asker may read and says what it could not reach; the code reads it once, never the answer.
    A message that names a time passes that window as ``date``; one that does not looks back ``PAST_ACTIVITY_DAYS`` days."""
    from datetime import date as calendar_date
    from quest_ai_runner.the_world.tools.file_reading.date_references import date_references_in

    if not operation or action not in ("answer", "clarify"):
        return []
    people = names_in(message)[:PAST_ACTIVITY_MAX_NAMES]
    references = date_references_in(message, today or calendar_date.today())
    if not people or (action != "clarify" and not references):
        return []
    window: Dict[str, Any] = {"days": PAST_ACTIVITY_DAYS}
    if references:
        first, last = references[0][1]
        window = {"date": f"{first.isoformat()}..{last.isoformat()}"}
    return [{"operation": operation, "args": {"person": name, "limit": 10, **window}} for name in people]


def deep_is_tool_sized(plan: Any, registry: Any) -> bool:
    """True when a planned deep run may be work a registered tool performs, so the planner is asked once to plan it as tool steps: one run (no staged
    ``deep_subtasks``), any target (a code or files target is asked once whether it is really a cue about a milestone or record), and a registry offering at least one tool that acts. A planner that still chooses deep after
    the hand-back is accepted. Facts about the plan and the tool set only, never a word of the request or of the plan's prose."""
    if registry is None or getattr(plan, "action", None) != "deep" or getattr(plan, "deep_subtasks", None):
        return False
    return any(getattr(spec, "mutates", False) for spec in registry.all())


def act_answered_without_a_write(plan: Any, user_intent: Optional[str], registry: Any, tool_calls_ran: bool) -> bool:
    """True when the planner's own structured verdict is "the person ordered a change" (``user_intent`` act), it chose to answer (not decline, support or queue the work; a question back before acting is handed back too, because what it asks is often readable), nothing was changed this turn, and a registered tool could change something. Facts about the plan and the tool set only."""
    if registry is None or tool_calls_ran or user_intent != "act" or getattr(plan, "action", None) != "answer":
        return False
    if (getattr(plan, "act_declined", False) or getattr(plan, "deferred_deep", None)
            or getattr(plan, "reply_shape", None) in ("decline", "support")):
        return False
    return any(getattr(spec, "mutates", False) for spec in registry.all())


def mutating_tool_ran(gathered: Iterable[Mapping[str, Any]], registry: Any) -> bool:
    """True when a tool that changes something has run this turn (its receipt is in what was gathered, locator ``tool:<name>``)."""
    if registry is None:
        return False
    for item in gathered:
        locator = str(item.get("locator") or "") if isinstance(item, Mapping) else ""
        if locator.startswith("tool:"):
            spec = registry.get(locator[len("tool:"):])
            if spec is not None and getattr(spec, "mutates", False):
                return True
    return False


ROUTE_DEEP, ROUTE_TOOL, ROUTE_INLINE = "deep", "tool", "inline"


def escalation_route(plan: Any, user_intent: Optional[str], registry: Any, handed_back: bool, wrote: bool) -> str:
    """Where an escalation to a deep run goes, from structured facts only: the plan's ``deep_target`` (what the deliverable lands on), its staged
    subtasks, the person's ``user_intent`` and the tool set. A deliverable that is text for the reply (``draft_in_chat``) is answered inline. Code
    or files, or staged work of many runs, is deep. Quest-record work (``quest_data``) or an order with no stated target, while a tool could act and
    none has yet, goes back to the planner once to plan a tool step; after that hand-back, or once a write ran, the answer stands. Anything else is deep."""
    target = getattr(plan, "deep_target", None) if plan is not None else None
    if target == "draft_in_chat" or user_intent == "ask":      # text for the reply, or a question: answered inline, never a background run
        return ROUTE_INLINE
    if target == "code_or_files" or (plan is not None and getattr(plan, "deep_subtasks", None)):
        return ROUTE_DEEP
    can_act = registry is not None and any(getattr(spec, "mutates", False) for spec in registry.all())
    if can_act and target in ("quest_data", None) and (target == "quest_data" or user_intent == "act"):
        if wrote:
            return ROUTE_INLINE
        return ROUTE_INLINE if handed_back else ROUTE_TOOL
    return ROUTE_DEEP


def tool_result_by_planner(gathered: Any) -> bool:
    """True when ``gathered`` holds a tool result the planner's own step produced. A read the code handed the turn at intake (``intake``, such as the quest's key measure) is not one."""
    return any(str(g.get("locator") or "").startswith("tool:") and not g.get("intake") for g in gathered or [] if isinstance(g, dict))
