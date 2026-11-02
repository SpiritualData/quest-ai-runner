"""The turn's work trace: what the run did for the reader, derived from STRUCTURAL results and emitted live.

A web search, a page read, a scholarly lookup, the records a turn drew on and the link check each become one step of a
``hubofhubs.ports.trace.WorkTrace`` (the contract, the allowlist projector and the throttle live in the hub). Everything
here reads counts, domains and titles out of the result a call returned; nothing reads the model's wording, the query,
a snippet or a page, and the projector drops anything that is not on its allowlist anyway.

Opt in per turn with ``context_meta["work_trace"] = {"audience": "owner" | "shared"}``; without it ``start`` returns None
and the loop behaves exactly as before. ``owner`` means the viewer owns the records and may see their titles; anything
else (the default for a missing or unknown audience) keeps records as counts only. Every method here swallows its own
errors: a trace is decoration around the reply and never breaks a turn.

Honest counts: ``read`` of a page is a page actually fetched (a site a search only listed is shown only when the reply cites it, and a
search that opened and cited nothing is left as a found count with no list: "searched, no page opened"); of a paper, a work the finished
answer cites (its DOI or full title appears in the reply, or its first author and year stand together there); of a record, a card the
answer drew on: the planner took it (a quote of the card's own text in ``facts_read``, its id or title named there, or it was fetched
by id) AND the finished reply draws on it (its title stands in the reply, a run of three of its words does, every rare word of its title does, or the reply shares three distinct rare words or numbers with the card's text; a word common in general English never counts).
``found`` is what a search listed. A card the context assembler merely selected is a candidate, not a record read, so an unrelated
question shows no records step, and a reply that found nothing in a record it looked at does not carry that record. A record is shown by
its human title only (``is_record_title``): never a conversation turn, a topic line or any text of a card; a card without such a title is
not counted. The records step is sent once, when the reply is final, because what the reply drew on is only known then.
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
#: A reply also draws on a record when it shares this many distinct RARE words (or numbers of two digits or more) with the card's own text:
#: a word common in general English ("practice", "second") says nothing about a card, so it never counts. Title words are judged separately.
SHARED_WORDS, SHARED_WORD_CHARS = 3, 4
#: A reply restates a record's words more loosely than a planner quotes them: this shorter run of the card's own words counts.
REPLY_WORDS, REPLY_CHARS = 3, 14
#: Names shorter than this are too common to prove a card was named.
MIN_NAME_CHARS = 5
MAX_CARD_TEXT = 40000
#: A record's title is a short name a person gave it, not a sentence of card text: bounds on its shape.
TITLE_MAX_CHARS, TITLE_MAX_WORDS = 60, 10


def flat(text: Any) -> str:
    """Lower case, punctuation and runs of space collapsed: the form two strings are compared in."""
    return re.sub(r"[\W_]+", " ", str(text or "").lower()).strip()


def canonical_id(card_id: Any) -> str:
    """The id a card goes by however the store or a vector hit spelled it: the ``card:`` namespace is dropped, so one card reaching the trace
    as ``x`` and as ``card:x`` is one record (counted once, its facts not read as naming two cards)."""
    text = str(card_id or "").strip()
    return text[len("card:"):] if text.lower().startswith("card:") else text


def is_record_title(title: Any, card_id: Any = "") -> bool:
    """True when ``title`` has the shape of a name a person gave a record: one short line of at most ``TITLE_MAX_WORDS`` words, no
    colon or semicolon (a "Topic: ..." or "User: ..." line is card or turn text), not cut off, not a sentence end, not the history
    placeholder and not the card's own id as written (a slug such as ``morning-pages-habit``; a spaced, capitalised name that merely reads like
    the slug, "Morning pages habit", is a title a person gave and stays). Judges the shape of a stored title; it reads no meaning out of anything."""
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    if not text or len(text) > TITLE_MAX_CHARS or len(text.split()) > TITLE_MAX_WORDS or text == HISTORY_TITLE:
        return False
    if re.search(r"[:;]|\.\.\.|\u2026", text) or text.endswith("."):
        return False
    # A title that re-spells the id in lower case ("q6 slug" for ``q6-slug``) is the id leaking; one with a capital ("Morning pages habit") is a name.
    return text.lower() != str(card_id or "").strip().lower() and not (flat(text) == flat(card_id) and text == text.lower())


def host_cited(domain: str, answer: str) -> bool:
    """True when the reply names this exact site (its host, with or without a leading ``www.``, in a link or in text). A sibling or
    parent host does not count: citing ``python.org`` does not cite ``docs.python.org``. Matches a known host against the reply."""
    host = str(domain or "").lower().strip(".")
    if not host:
        return False
    return bool(re.search(r"(?:(?<![\w.-])|(?<=www\.))" + re.escape(host) + r"(?![\w-]|\.\w)", str(answer or "").lower()))


#: Whether a word is rare in general English. The brain knows no word list: the package registers one (``set_word_rarity``, from the
#: general-English bands the context adapters already use). Unregistered, no word counts as rare, so only a record's title can show it.
_rare_word = lambda word: False  # noqa: E731


def set_word_rarity(is_rare: Any) -> None:
    """Register ``is_rare(word) -> bool``: True for a word absent from common English (it can single out one record)."""
    global _rare_word
    _rare_word = is_rare


def distinctive_words(text: Any) -> set:
    """The rare words of ``text`` (``SHARED_WORD_CHARS`` characters or more, absent from common English) and its numbers of two digits or
    more: the part of a card that can tell it apart from any other text."""
    return {w for w in flat(text).split() if (w.isdigit() and len(w) >= 2) or (len(w) >= SHARED_WORD_CHARS and not w.isdigit() and _rare_word(w))}


def title_stands_in(card: Mapping[str, Any], answer: str) -> bool:
    """True when every word of the card's title of ``SHARED_WORD_CHARS`` characters or more stands in the reply and at least one of them is rare:
    a reply that names the record's subject. One rare word of a longer title ("pages" of "Morning pages habit") is not the subject."""
    words = {w for w in flat(card.get("title")).split() if len(w) >= SHARED_WORD_CHARS and not w.isdigit()}
    return any(_rare_word(w) for w in words) and words <= set(flat(answer).split())


def reply_draws_on(card: Mapping[str, Any], facts: Any, answer: str) -> bool:
    """True when the finished reply draws on this card: its title stands in the reply, a run of ``REPLY_WORDS`` words of the card's text
    does, every rare word of its title does, the reply shares ``SHARED_WORDS`` distinct rare words or numbers with the card's text (a paraphrase of it), or a run of words of a fact that took something from the card does. A record the planner read but the reply left out (or said
    it found nothing in) shares none of these. Compares stored text with the reply; it reads no meaning out of the reply."""
    title = flat(card.get("title"))
    if len(title) >= MIN_NAME_CHARS and " " + title + " " in " " + flat(answer) + " ":
        return True
    text = card.get("text") or ""
    if quoted_from(answer, text, REPLY_WORDS, REPLY_CHARS):
        return True
    if title_stands_in(card, answer):
        return True
    if len(distinctive_words(text) & distinctive_words(answer)) >= SHARED_WORDS:
        return True
    return any(quoted_from(answer, f, REPLY_WORDS, REPLY_CHARS) for f in facts or [])


def quoted_from(fact: str, card_text: str, words_n: int = QUOTE_WORDS, chars_n: int = QUOTE_CHARS) -> bool:
    """True when ``fact`` holds a run of ``words_n`` consecutive words (at least ``chars_n`` characters) that stands verbatim in ``card_text``.
    Verifies a quote against the stored text; it reads no meaning out of the fact."""
    words, haystack = flat(fact).split(), " " + flat(card_text) + " "
    for i in range(len(words) - words_n + 1):
        window = " ".join(words[i:i + words_n])
        if len(window) >= chars_n and " " + window + " " in haystack:
            return True
    return False


def cards_cited(facts: Any, cards: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """Ids of the ``cards`` (``{id: {"title", "text"}}``) the planner's ``facts_read`` took something from: a quote found in the
    card's text, or the card's id or full title named in the fact (a cited id verified against the known ids)."""
    out: List[str] = []
    facts = [f for f in (facts or []) if isinstance(f, str) and f.strip()]
    cards = {canonical_id(k): v for k, v in cards.items()}   # one card under two ids is one card
    for card_id, card in cards.items():
        names = [flat(card_id), flat(card.get("title"))]
        names = [n for n in names if len(n) >= MIN_NAME_CHARS]
        for fact in facts:
            f = " " + flat(fact) + " "
            if any(" " + n + " " in f for n in names) or quoted_from(fact, card.get("text") or ""):
                out.append(card_id)
                break
    return out


def facts_about(card_id: str, facts: Any, cards: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """The facts that took something from THIS card alone: a fact that names or quotes several cards (a remark about which hits were weak, a
    list of titles) is not a quote of any one of them, so its words say nothing about whether the reply used this card."""
    return [f for f in (facts or []) if isinstance(f, str) and cards_cited([f], cards) == [canonical_id(card_id)]]


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
        self.records: Dict[str, Dict[str, str]] = {}       # the records the answer drew on: the only ones ever counted or shown, set when the reply is final
        self.taken: Dict[str, Dict[str, str]] = {}         # cards the planner took a fact from or the run fetched by id: candidates the reply must still draw on
        self.taken_facts: Dict[str, List[str]] = {}        # the facts that took something from each card
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
        """The step item for one projected card (its human title only), or None for a history turn or a card without a title (not shown, not counted)."""
        if not isinstance(card, Mapping) or str(card.get("adapter") or "").lower() == HISTORY_ADAPTER:
            return None
        card_id = canonical_id(card.get("id"))
        # The card's own short name when the assembler gave one (a vector hit's ``title`` is a slice of the embedded text, often the body).
        title = next((t for t in (re.sub(r"\s+", " ", str(card.get(key) or "")).strip() for key in ("name", "title"))
                      if card_id and is_record_title(t, card_id)), "")
        if not title:
            return None
        item = {"ref": card_id, "label": title}
        kinds = card.get("item_type_counts") or {}
        kind = next((k for k in RECORD_KINDS if k in kinds), "")
        if kind:
            item["kind"] = kind
        return item

    @safe
    def records_selected(self, card_metadata: Any, cards: Any = None) -> None:
        """Cards the context assembly selected for the turn, already projected by ``_project_card_metadata_for_event``. They are
        only candidates: nothing is shown until the answer draws on one (``facts_noted``, ``records_used``, then ``finish``). ``cards`` are the
        same cards with their text (``rendered_section`` and the items' ``text``), kept in process to verify a quote against."""
        with self.lock:
            for card in card_metadata or []:
                item = self.record_item(card)
                if item:
                    self.candidates.setdefault(item["ref"], item)
            self.keep_texts(cards, self.candidates)

    def push_records(self) -> None:
        if self.records:
            self.trace.update(STEP_RECORDS, solution_id=SOLUTION_RECORDS, kind="records", label_key=LABEL_RECORDS,
                              state="done", counts={"found": len(self.records), "read": len(self.records)},
                              items=list(self.records.values()))

    @safe
    def records_used(self, card_metadata: Any, cards: Any = None) -> None:
        """Cards the run fetched by id: the model asked for that record. Still only a candidate until the reply draws on it. ``cards`` are
        the same cards with their text, kept in process to check the reply against."""
        with self.lock:
            for card in card_metadata or []:
                # A fetch by id names only the id: the title comes from the assembled card of the same id, and a card with none is dropped.
                item = self.candidates.get(canonical_id(card.get("id"))) if isinstance(card, Mapping) else None
                item = item or self.record_item(card)
                if item:
                    self.taken.setdefault(item["ref"], item)
            self.keep_texts(cards, self.taken)

    def keep_texts(self, cards: Any, known: Mapping[str, Any]) -> None:
        for card in cards or []:
            if not isinstance(card, Mapping) or canonical_id(card.get("id")) not in known:
                continue
            parts = [str(card.get("rendered_section") or "")] + [
                str(i.get("text") or "") for i in (card.get("items") or []) if isinstance(i, Mapping)]
            text = "\n".join(p for p in parts if p)[:MAX_CARD_TEXT]
            self.card_texts[canonical_id(card["id"])] = {"title": str(card.get("title") or ""), "text": text}

    @safe
    def facts_noted(self, facts: Any) -> None:
        """The planner's ``facts_read`` of one decision: each card a fact quotes or names becomes a card the planner took something from."""
        with self.lock:
            facts = [f for f in (facts or []) if isinstance(f, str) and f.strip()]
            pool = {cid: self.card_texts.get(cid, {"title": item["label"], "text": ""}) for cid, item in self.candidates.items()}
            for card_id in cards_cited(facts, pool):
                self.taken.setdefault(card_id, self.candidates[card_id])
                for fact in facts_about(card_id, facts, pool):
                    self.taken_facts.setdefault(card_id, []).append(fact)

    def settle_records(self, answer: str) -> None:
        """The reply is final: of the cards the planner took, keep those the reply draws on, and send the records step once."""
        for card_id, item in self.taken.items():
            card = self.card_texts.get(card_id) or {}
            known = {"title": item["label"], "text": card.get("text") or ""}
            if reply_draws_on(known, self.taken_facts.get(card_id), answer):
                self.records[card_id] = item
        self.push_records()

    # -- the link check ------------------------------------------------------------------------

    @safe
    def links_checked(self, link_checks: Any) -> None:
        """``link_checks`` are the guard's verdict dicts. Only the DOMAIN of a link that was left out leaves (one item per removed link, so
        the person can tell which link is missing); the path, query string and every link that checked out stay behind."""
        checks = [c for c in (link_checks or []) if isinstance(c, Mapping)]
        if not checks or self.sent_links:
            return
        self.sent_links = True
        left_out = [c for c in checks if str(c.get("verdict") or "").upper() != "OK"]
        items = [{"ref": domain, "label": domain, "kind": "removed"} for domain in (domain_of(c.get("url")) for c in left_out) if domain]
        self.trace.update(STEP_LINKS, solution_id=SOLUTION_LINKS, kind="check", label_key=LABEL_LINKS, state="done",
                          counts={"checked": len(checks), "failed": len(left_out)}, items=items, affects_user=bool(left_out))

    @safe
    def finish(self, answer: Any = None) -> None:
        """The turn is ending: a step still marked running is closed with what it has. Given the finished reply, each step settles on what
        the reply used. Web: only sites the reply names are listed (a page that was opened is always a read page); a search that opened
        no page keeps its found count and the sites it listed (domains, capped by the projector), which the screen says as found, not read. Papers: the works
        the reply cites are read, the rest of the search is only found. Records: the cards the planner took AND the reply draws on."""
        with self.lock:
            w = self.web
            if w["inflight"]:
                w["inflight"] = 0
                self.push_web()
            p = self.papers
            if answer is not None:
                text = str(answer)
                # A search that opened no page keeps every site it listed (the projector caps the list): the screen says them as found, never read.
                if w["found_items"] and w["read_items"]:
                    w["found_items"] = {d: i for d, i in w["found_items"].items() if d in w["read_items"] or host_cited(d, text)}
                if w["found"] or w["read"] or w["failed"]:
                    self.push_web()
                self.settle_records(text)
            was_running = bool(p["inflight"])
            p["inflight"] = 0
            if answer is not None and p["works"]:
                cited = [ref for ref, work in p["works"].items() if paper_cited(work, str(answer))]
                p["items"] = {ref: p["items"][ref] for ref in cited}
                p["read"] = len(cited)
                self.push_papers()
            elif was_running:
                self.push_papers()
