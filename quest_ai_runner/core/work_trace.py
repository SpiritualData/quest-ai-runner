"""The turn's work trace: what the run did for the reader, derived from STRUCTURAL results and emitted live.

A web search, a page read, a scholarly lookup, the records a turn drew on and the link check each become one step of a
``hubofhubs.ports.trace.WorkTrace`` (the contract, the allowlist projector and the throttle live in the hub). Everything
here reads counts, domains and titles out of the result a call returned; nothing reads the model's wording, the query,
a snippet or a page, and the projector drops anything that is not on its allowlist anyway.

Opt in per turn with ``context_meta["work_trace"] = {"audience": "owner" | "shared"}``; without it ``start`` returns None
and the loop behaves exactly as before. ``owner`` means the viewer owns the records and may see their titles; anything
else (the default for a missing or unknown audience) keeps records as counts only. Every method here swallows its own
errors: a trace is decoration around the reply and never breaks a turn.

Honest counts: ``read`` of a page is a page actually fetched (a site a search only listed is shown only when the reply cites it); of a paper, a work the finished answer cites (its DOI or full
title appears in the reply, or its first author and year stand together there); of a record, a card the answer drew on
(the planner quoted it in ``facts_read`` and that quote is found in the card's own text, or the planner named its id or title,
or it was fetched by id). ``found`` is what a search listed. A card the context assembler merely selected is a candidate, not a
record read, so an unrelated question shows no records step.
"""
from __future__ import annotations

import re
import threading
import uuid
from typing import Any, Dict, List, Mapping, Optional

from esoi import implementation
from hubofhubs.ports.deep import ProgressEvent  # noqa: F401  (the event the emit callable receives)
from hubofhubs.ports.trace import WorkTrace, domain_of

STEP_WEB, STEP_PAPERS, STEP_RECORDS, STEP_LINKS = "web", "papers", "records", "links"
SOLUTION_WEB, SOLUTION_PAPERS, SOLUTION_RECORDS, SOLUTION_LINKS = (
    "search-backend", "know-the-world", "context-assembler", "watch-for-risk")
LABEL_WEB, LABEL_PAPERS, LABEL_RECORDS, LABEL_LINKS = (
    "workTrace.web.reading", "workTrace.papers.searching", "workTrace.records.looking", "workTrace.links.checking")
SCHOLARLY_TOOL = "search_scientific_literature"
RECORD_KINDS = ("quest", "reflection", "goal", "note")
#: A conversation-history card is not one of the person's records.
HISTORY_ADAPTER = "turn"
HISTORY_TITLE = "Conversation turn"
#: A quoted fact counts as taken from a card when this many consecutive words (at least this many characters) stand in the card's text.
QUOTE_WORDS, QUOTE_CHARS = 4, 18
#: Names shorter than this are too common to prove a card was named.
MIN_NAME_CHARS = 5
MAX_CARD_TEXT = 40000


def flat(text: Any) -> str:
    """Lower case, punctuation and runs of space collapsed: the form two strings are compared in."""
    return re.sub(r"[\W_]+", " ", str(text or "").lower()).strip()


def quoted_from(fact: str, card_text: str) -> bool:
    """True when ``fact`` holds a run of ``QUOTE_WORDS`` consecutive words that stands verbatim in ``card_text``.
    Verifies a quote against the stored text; it reads no meaning out of the fact."""
    words, haystack = flat(fact).split(), " " + flat(card_text) + " "
    for i in range(len(words) - QUOTE_WORDS + 1):
        window = " ".join(words[i:i + QUOTE_WORDS])
        if len(window) >= QUOTE_CHARS and " " + window + " " in haystack:
            return True
    return False


def cards_cited(facts: Any, cards: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """Ids of the ``cards`` (``{id: {"title", "text"}}``) the planner's ``facts_read`` took something from: a quote found in the
    card's text, or the card's id or full title named in the fact (a cited id verified against the known ids)."""
    out: List[str] = []
    facts = [f for f in (facts or []) if isinstance(f, str) and f.strip()]
    for card_id, card in cards.items():
        names = [flat(card_id), flat(card.get("title"))]
        names = [n for n in names if len(n) >= MIN_NAME_CHARS]
        for fact in facts:
            f = " " + flat(fact) + " "
            if any(" " + n + " " in f for n in names) or quoted_from(fact, card.get("text") or ""):
                out.append(card_id)
                break
    return out


def paper_cited(work: Mapping[str, Any], answer: str) -> bool:
    """True when the finished reply cites this work: its DOI or its full title stands in the reply, or its first author's surname
    and its year both stand within one short stretch of it (a citation line). Matches known values of the result against the
    reply; it reads no meaning out of the reply."""
    text = flat(answer)
    doi = bare_doi(work.get("doi")).lower()
    if doi and doi in str(answer).lower():
        return True
    title = flat(work.get("title"))
    if len(title) >= 12 and title in text:
        return True
    names = [str(a).strip() for a in (work.get("authors") or []) if str(a or "").strip()]
    surname = flat(names[0].split()[-1]) if names else ""
    year = str(work.get("year") or "")
    if len(surname) >= 3 and year:
        for m in re.finditer(re.escape(surname), text):
            if year in text[max(0, m.start() - 40): m.end() + 40]:
                return True
    return False


def safe(method):
    """A trace method never raises into the run."""
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception:  # noqa: BLE001
            return None
    wrapped.__name__ = method.__name__
    wrapped.__doc__ = method.__doc__
    return wrapped


def start(emit: Any, context_meta: Optional[Mapping[str, Any]]) -> Optional["TurnTrace"]:
    """The turn's trace when the consumer opted in through ``context_meta["work_trace"]``, else None."""
    setting = (context_meta or {}).get("work_trace")
    if not isinstance(setting, Mapping):
        return None
    owner = str(setting.get("audience") or "").strip().lower() == "owner"
    trace_id = str(setting.get("trace_id") or "") or uuid.uuid4().hex[:24]
    return TurnTrace(lambda event: emit.emit(event), trace_id=trace_id, viewer_may_see_titles=owner)


def surname_citation(authors: Any, year: Any) -> str:
    """``Lee et al., 2021`` style line from a list of author names and a year; empty parts are left out."""
    names = [str(a).strip() for a in (authors or []) if str(a or "").strip()]
    last = [n.split()[-1] for n in names]
    if not last:
        who = ""
    elif len(last) == 1:
        who = last[0]
    elif len(last) == 2:
        who = f"{last[0]} and {last[1]}"
    else:
        who = f"{last[0]} et al."
    return ", ".join(p for p in (who, str(year) if year else "") if p)


def bare_doi(value: Any) -> str:
    return re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", str(value or "").strip(), flags=re.IGNORECASE)


@implementation("show-the-ais-work", "trail")
class TurnTrace:
    """One turn's accumulated steps. Thread-safe: parallel reads update the same step."""

    def __init__(self, emit, *, trace_id: str, viewer_may_see_titles: bool):
        self.trace = WorkTrace(emit, trace_id=trace_id, viewer_may_see_titles=viewer_may_see_titles)
        self.lock = threading.Lock()
        self.web: Dict[str, Any] = {"found": 0, "read": 0, "failed": 0, "inflight": 0, "ok": 0, "read_items": {}, "found_items": {}}
        self.papers: Dict[str, Any] = {"found": 0, "read": 0, "failed": 0, "inflight": 0, "ok": 0, "items": {}, "titles": set(), "works": {}}
        self.records: Dict[str, Dict[str, str]] = {}       # the records the answer drew on: the only ones ever counted or shown
        self.candidates: Dict[str, Dict[str, str]] = {}    # cards the assembler selected: shown to nobody
        self.card_texts: Dict[str, Dict[str, str]] = {}    # id -> {"title", "text"}, kept in process to verify quotes, never sent
        self.final = False
        self.sent_links = False

    # -- web -----------------------------------------------------------------------------------

    def push_web(self, force_state: Optional[str] = None) -> None:
        w = self.web
        state = force_state or ("running" if w["inflight"] > 0 else ("failed" if w["failed"] and not w["ok"] else "done"))
        # ``kind`` tells the screen which domains were opened and which a search only listed, so a listed site is never shown as read.
        items = ([dict(i, kind="read") for i in w["read_items"].values()]
                 + [dict(i, kind="found") for d, i in w["found_items"].items() if d not in w["read_items"]])
        counts = {"found": w["found"], "read": w["read"]}
        if w["failed"]:
            counts["failed"] = w["failed"]
        self.trace.update(STEP_WEB, solution_id=SOLUTION_WEB, kind="sources", label_key=LABEL_WEB, state=state,
                          counts=counts, items=items)

    @safe
    def web_started(self) -> None:
        with self.lock:
            self.web["inflight"] += 1
            self.push_web()

    @safe
    def web_searched(self, observation: Any) -> None:
        with self.lock:
            w = self.web
            w["inflight"] = max(0, w["inflight"] - 1)
            if getattr(observation, "kind", "error") == "error":
                w["failed"] += 1
            else:
                w["ok"] += 1
                for hit in getattr(observation, "hits", None) or []:
                    if not isinstance(hit, Mapping):
                        continue
                    domain = domain_of(hit.get("url"))
                    if not domain:
                        continue
                    w["found"] += 1
                    w["found_items"].setdefault(domain, {"ref": domain, "label": str(hit.get("title") or domain)})
            self.push_web()

    @safe
    def web_page_read(self, url: Any, observation: Any) -> None:
        with self.lock:
            w = self.web
            w["inflight"] = max(0, w["inflight"] - 1)
            domain = domain_of(url)
            if getattr(observation, "kind", "error") == "error" or not domain:
                w["failed"] += 1
            else:
                w["ok"] += 1
                w["read"] += 1
                title = (w["found_items"].get(domain) or {}).get("label") or domain
                w["read_items"].setdefault(domain, {"ref": domain, "label": title})
            self.push_web()

    # -- scholarly papers ----------------------------------------------------------------------

    def push_papers(self) -> None:
        p = self.papers
        state = "running" if p["inflight"] > 0 else ("failed" if p["failed"] and not p["ok"] else "done")
        counts = {"found": p["found"], "read": p["read"]}
        if p["failed"]:
            counts["failed"] = p["failed"]
        self.trace.update(STEP_PAPERS, solution_id=SOLUTION_PAPERS, kind="papers", label_key=LABEL_PAPERS, state=state,
                          counts=counts, items=list(p["items"].values()))

    @safe
    def papers_started(self) -> None:
        with self.lock:
            self.papers["inflight"] += 1
            self.push_papers()

    @safe
    def papers_searched(self, result: Any) -> None:
        """``result`` is the ``ToolResult`` of the literature search: ``data = {"works": [...], "total": n}``."""
        with self.lock:
            p = self.papers
            p["inflight"] = max(0, p["inflight"] - 1)
            data = getattr(result, "data", None)
            works = (data or {}).get("works") if isinstance(data, Mapping) else None
            if not getattr(result, "ok", False) or not isinstance(works, list):
                p["failed"] += 1
            else:
                p["ok"] += 1
                total = (data or {}).get("total")
                matched = int(total) if isinstance(total, int) and total >= len(works) else len(works)
                for work in works:
                    if not isinstance(work, Mapping) or not work.get("title"):
                        continue
                    ref = bare_doi(work.get("doi")) or f"work-{len(p['items']) + 1}"
                    same_title = re.sub(r"\W+", " ", str(work["title"]).lower()).strip()
                    if ref in p["items"] or same_title in p["titles"]:
                        continue   # the same work from a second search, or listed under a second DOI, is read once: a count never exceeds the lines shown
                    p["titles"].add(same_title)
                    item = {"ref": ref, "label": str(work["title"])}
                    sub = surname_citation(work.get("authors"), work.get("year"))
                    if sub:
                        item["sub"] = sub
                    p["items"][ref] = item
                    p["works"][ref] = work
                    p["read"] += 1
                # Searches match overlapping sets, so their totals cannot be added: the largest one is the most the run
                # can say it found without counting a paper twice.
                p["found"] = max(p["found"], matched, p["read"])
            self.push_papers()

    # -- the person's records ------------------------------------------------------------------

    def record_item(self, card: Mapping[str, Any]) -> Optional[Dict[str, str]]:
        """The step item for one projected card, or None for a history turn (not one of the person's records)."""
        if not isinstance(card, Mapping) or str(card.get("adapter") or "").lower() == HISTORY_ADAPTER:
            return None
        title = str(card.get("title") or "")
        card_id = str(card.get("id") or "")
        if not card_id or title == HISTORY_TITLE:
            return None
        item = {"ref": card_id, "label": title or card_id}
        kinds = card.get("item_type_counts") or {}
        kind = next((k for k in RECORD_KINDS if k in kinds), "")
        if kind:
            item["kind"] = kind
        return item

    @safe
    def records_selected(self, card_metadata: Any, cards: Any = None) -> None:
        """Cards the context assembly selected for the turn, already projected by ``_project_card_metadata_for_event``. They are
        only candidates: nothing is shown until the answer draws on one (``facts_noted``, ``records_used``). ``cards`` are the
        same cards with their text (``rendered_section`` and the items' ``text``), kept in process to verify a quote against."""
        with self.lock:
            for card in card_metadata or []:
                item = self.record_item(card)
                if item:
                    self.candidates.setdefault(item["ref"], item)
            for card in cards or []:
                if not isinstance(card, Mapping) or str(card.get("id") or "") not in self.candidates:
                    continue
                parts = [str(card.get("rendered_section") or "")] + [
                    str(i.get("text") or "") for i in (card.get("items") or []) if isinstance(i, Mapping)]
                text = "\n".join(p for p in parts if p)[:MAX_CARD_TEXT]
                self.card_texts[str(card["id"])] = {"title": str(card.get("title") or ""), "text": text}

    def push_records(self) -> None:
        if self.records:
            self.trace.update(STEP_RECORDS, solution_id=SOLUTION_RECORDS, kind="records", label_key=LABEL_RECORDS,
                              state="done", counts={"found": len(self.records), "read": len(self.records)},
                              items=list(self.records.values()))

    @safe
    def records_used(self, card_metadata: Any) -> None:
        """Cards the run fetched by id: the model asked for that record, so it was read."""
        with self.lock:
            for card in card_metadata or []:
                item = self.record_item(card)
                if item:
                    self.records.setdefault(item["ref"], self.candidates.get(item["ref"]) or item)
            self.push_records()

    @safe
    def facts_noted(self, facts: Any) -> None:
        """The planner's ``facts_read`` of one decision: each card a fact quotes or names becomes a record the answer drew on."""
        with self.lock:
            pool = {cid: self.card_texts.get(cid, {"title": item["label"], "text": ""}) for cid, item in self.candidates.items()
                    if cid not in self.records}
            hit = cards_cited(facts, pool)
            for card_id in hit:
                self.records[card_id] = self.candidates[card_id]
            if hit:
                self.push_records()

    # -- the link check ------------------------------------------------------------------------

    @safe
    def links_checked(self, link_checks: Any) -> None:
        """``link_checks`` are the guard's verdict dicts. Counts only: the addresses never leave."""
        checks = [c for c in (link_checks or []) if isinstance(c, Mapping)]
        if not checks or self.sent_links:
            return
        self.sent_links = True
        removed = sum(1 for c in checks if str(c.get("verdict") or "").upper() != "OK")
        self.trace.update(STEP_LINKS, solution_id=SOLUTION_LINKS, kind="check", label_key=LABEL_LINKS, state="done",
                          counts={"checked": len(checks), "failed": removed}, affects_user=removed > 0)

    @safe
    def finish(self, answer: Any = None) -> None:
        """The turn is ending: a step still marked running is closed with what it has. Given the finished reply, the papers step
        settles on the works the reply cites: those are read, the rest of the search is only found."""
        with self.lock:
            if self.web["inflight"]:
                self.web["inflight"] = 0
                self.push_web()
            p = self.papers
            if answer is not None and self.web["found_items"]:
                # Sites a search only listed stay on screen only when the reply cites them (its address host stands in the reply):
                # a listed site the answer never used says nothing the person can act on. Opened pages are always kept.
                text = str(answer).lower()
                w = self.web
                w["found_items"] = {d: i for d, i in w["found_items"].items() if d in w["read_items"] or d.lower() in text}
                self.push_web()
            was_running = bool(p["inflight"])
            p["inflight"] = 0
            if answer is not None and p["works"]:
                cited = [ref for ref, work in p["works"].items() if paper_cited(work, str(answer))]
                p["items"] = {ref: p["items"][ref] for ref in cited}
                p["read"] = len(cited)
                self.push_papers()
            elif was_running:
                self.push_papers()
