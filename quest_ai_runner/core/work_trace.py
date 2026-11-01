"""The turn's work trace: what the run did for the reader, derived from STRUCTURAL results and emitted live.

A web search, a page read, a scholarly lookup, the records a turn drew on and the link check each become one step of a
``hubofhubs.ports.trace.WorkTrace`` (the contract, the allowlist projector and the throttle live in the hub). Everything
here reads counts, domains and titles out of the result a call returned; nothing reads the model's wording, the query,
a snippet or a page, and the projector drops anything that is not on its allowlist anyway.

Opt in per turn with ``context_meta["work_trace"] = {"audience": "owner" | "shared"}``; without it ``start`` returns None
and the loop behaves exactly as before. ``owner`` means the viewer owns the records and may see their titles; anything
else (the default for a missing or unknown audience) keeps records as counts only. Every method here swallows its own
errors: a trace is decoration around the reply and never breaks a turn.

Honest counts: ``read`` is a page actually fetched or a work whose abstract the run actually took; ``found`` is what a
search listed.
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
        self.papers: Dict[str, Any] = {"found": 0, "read": 0, "failed": 0, "inflight": 0, "ok": 0, "items": {}, "titles": set()}
        self.records: Dict[str, Dict[str, str]] = {}
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
                    p["read"] += 1
                # Searches match overlapping sets, so their totals cannot be added: the largest one is the most the run
                # can say it found without counting a paper twice.
                p["found"] = max(p["found"], matched, p["read"])
            self.push_papers()

    # -- the person's records ------------------------------------------------------------------

    @safe
    def records_used(self, card_metadata: Any) -> None:
        """Cards a context assembly selected, already projected by ``_project_card_metadata_for_event``. History cards
        (the conversation itself) are not records. The titles are the cards' titles; the projector hides them from a
        viewer who may not see them."""
        with self.lock:
            for card in card_metadata or []:
                if not isinstance(card, Mapping) or str(card.get("adapter") or "").lower() == HISTORY_ADAPTER:
                    continue
                title = str(card.get("title") or "")
                card_id = str(card.get("id") or "")
                if not card_id or title == HISTORY_TITLE:
                    continue
                item = {"ref": card_id, "label": title or card_id}
                kinds = card.get("item_type_counts") or {}
                kind = next((k for k in RECORD_KINDS if k in kinds), "")
                if kind:
                    item["kind"] = kind
                self.records.setdefault(card_id, item)
            if self.records:
                self.trace.update(STEP_RECORDS, solution_id=SOLUTION_RECORDS, kind="records", label_key=LABEL_RECORDS,
                                  state="done", counts={"found": len(self.records), "read": len(self.records)},
                                  items=list(self.records.values()))

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
    def finish(self) -> None:
        """The turn is ending: a step still marked running is closed with what it has."""
        with self.lock:
            if self.web["inflight"]:
                self.web["inflight"] = 0
                self.push_web()
            if self.papers["inflight"]:
                self.papers["inflight"] = 0
                self.push_papers()
