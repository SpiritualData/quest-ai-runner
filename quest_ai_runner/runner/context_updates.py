"""context_updates -- ONE place that answers "what has changed since an assistant last looked?"

THE PROBLEM THIS EXISTS TO FIX. Every context channel in this library was built the same way and
wired by hand twice. ``runner.reflections`` reads what the person wrote; ``runner.insights`` reads
what they captured; ``executor._build_context_view`` reads the quest's notes and run history;
``AutopilotPass._reflections``/``._insights`` read two of those again, separately, with their own
caching and their own slot in ``compose_batch_text``. Each was a good module. Together they meant
that adding a THIRD channel (a comment on a document, a folder the work owns, a category of
capture that matters to one quest) was not "register a source", it was "write a module, write a
fetch method on two callers, write a render function, add a parameter to the composer, and
remember to tell the run to look at it". So people did the last part in the prompt instead: the
quest's standing instructions grew lines like "check the comments on the doc", "look at my
insights tagged X" -- a person hand-maintaining, in prose, the retrieval plan that the context
engine was supposed to own.

THE SHAPE HERE. A ``ContextSource`` answers one question: given a card and a watermark, what has
arrived since? An ``UpdateEngine`` holds the registry, resolves WHICH sources a given card uses
(from data the card carries, never from a hardcoded map), runs them, and returns a
``ContextUpdates`` bundle. Every caller -- the autopilot pass composing a brief, the executor
building a task's context view, an attended session -- asks the same engine the same question, so
a source added once is seen by all of them.

CUSTOM PER CARD, AS DATA. What a card watches is a list of specs the card itself carries:
``{"source": "insights", "categories": ["PhD"]}``, ``{"source": "drive_comments",
"folder_id": "1kEc..."}``, ``{"source": "quest_notes"}``. An assistant can write a spec (it is
JSON, and the vocabulary is discoverable via ``UpdateEngine.describe_sources()``); nothing in this
module hardcodes that any particular card watches any particular thing, and an unknown source name
degrades to a reported gap rather than an exception.

A TAG NEVER GATES DELIVERY. ``runner.insights`` refuses to match tags against quest names for a
good reason (hard rule #3): a fixed string rule silently loses every capture whose wording it did
not anticipate. So a ``categories`` spec here does not filter the captures. Every capture is its
own update with its own ref; a tag match only FLAGS one as waiting on an answer. The only thing
that sets a capture aside is the relevance judge, which is a model reading the card's subject
matter, and any failure of the judge keeps everything.

THE RECEIPT. Surfacing context is only half the loop: a person who leaves a comment or captures an
insight has no way to know whether the run that followed actually used it, and "I read your note"
buried in three paragraphs of result is not an answer. So every offered update carries a short
stable ref (``[U1]``), the prompt block asks the run to close with one line per ref saying in a few
words how it used it, and ``render_receipt`` turns the run's OWN answer into a fixed-format block
appended to the result. That is deliberately not a second model call grading the first: a summary
produced by another pass would be a guess about a run it did not do, would cost a call per run, and
would be exactly the afterthought this is meant not to be. The run that used the material is the
only thing that knows how it used it. When a run says nothing about a ref, the receipt says so
plainly rather than inventing a use.

Everything here is best-effort: a source that raises is reported as a failed source and the rest of
the bundle is delivered. A card with no specs, a client missing methods, an unreachable API and an
empty inbox all produce an EMPTY bundle, which every caller treats as "nothing new" rather than as
a failure.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .context_update_types import (
    BLOCK_END,
    BLOCK_START,
    CACHE_TTL_SECONDS,
    ContextUpdate,
    DEFAULT_ALWAYS,
    FIRST_LOOK_DAYS,
    MAX_BODY_CHARS,
    MAX_OPEN_PER_SOURCE,
    MAX_PERSON_BODY_CHARS,
    MAX_UPDATES,
    OPEN_ITEM_MAX_AGE_DAYS,
    PERSON_BODY_BUDGET,
    SourceGap,
    SourceReport,
    _as_utc,
    _utcnow,
    log,
)
from .context_update_receipts import (
    append_receipt,
    block_spans,
    parse_dispositions,
    parse_manifest,
    parse_usage_notes,
    render_receipt,
    split_disposition,
    strip_usage_block,
    usage_receipt_gate,
)
from .context_update_state import (  # noqa: F401
    CollectRequest,
    RelayedItems,
    Watermarks,
    _card_label,
    relay_log_path_for,
)
from .context_update_bundle import (  # noqa: F401
    ContextUpdates,
)
from .context_sources_base import ContextSource, _reply_channel
from .context_sources_quest import (
    CollectionEntriesSource,
    DecisionsSource,
    GoalUpdatesSource,
    InsightsSource,
    QuestEventsSource,
    QuestNotesSource,
    ReflectionsSource,
)
from .context_sources_external import (
    DriveChangesSource,
    DriveCommentsSource,
    GoogleChatSource,
    InboundMailSource,
    NotionDatabaseSource,
)
from .context_update_judges import _describe_work, llm_admission_judge, llm_relevance_judge


# ---------------------------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------------------------

# How a card says what it watches, when the caller supplies no resolver. Read from the card row
# itself so a backend that grows the field needs no code change here, and so an assistant can add
# a source by writing data rather than by editing this library.
DEFAULT_SPEC_KEYS = ("context_sources", "watch")


def default_spec_resolver(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The specs a card carries, from the card itself, or [] when it declares none.

    Looks in the card's own fields and inside its ``autopilot`` settings block (where per-quest
    configuration already lives in this library). Accepts a bare source name as shorthand:
    ``["quest_notes", {"source": "insights", "categories": ["PhD"]}]``.
    """
    found: List[Any] = []
    for holder in (card or {}, (card or {}).get("autopilot") or {}):
        if not isinstance(holder, dict):
            continue
        for key in DEFAULT_SPEC_KEYS:
            value = holder.get(key)
            if isinstance(value, list):
                found.extend(value)
    return _normalize_specs(found)


def _normalize_specs(entries: Sequence[Any]) -> List[Dict[str, Any]]:
    """``["quest_notes", {"source": "insights", ...}]`` -> a list of dicts, junk dropped."""
    specs: List[Dict[str, Any]] = []
    for entry in entries or []:
        if isinstance(entry, str) and entry.strip():
            specs.append({"source": entry.strip()})
        elif isinstance(entry, dict) and str(entry.get("source") or "").strip():
            specs.append(dict(entry))
    return specs


def consumer_spec_resolver(
        spec_map: Optional[Dict[str, List[Dict[str, Any]]]] = None
) -> Callable[[Dict[str, Any]], List[Dict[str, Any]]]:
    """A resolver that reads the card's own specs AND a consumer-supplied ``{card_id: specs}`` map.

    The card carrying its own specs is the design (see the module docstring), and it stays the
    design: the map is merged with whatever the card declares, not substituted for it.

    The map exists because a card cannot always carry them YET. A backend has to grow the field
    before a person can set it, and until it does, every deployment is blocked on a schema change
    in another service to use any of this. Live case: quest-backend's autopilot settings reject an
    unknown key outright (422 ``extra_forbidden``), so a quest could not name a folder to watch at
    all. A consumer-side map unblocks that the same way ``quest_folder_map`` already does for a
    quest's local folder, and it keeps working afterwards as a per-deployment default under the
    card's own choices.

    Precedence, when both name the same source: the CARD wins. What a person set on the thing
    itself is more specific than what a deployment configured for it, and a config file quietly
    overriding a person's own setting is the failure this ordering exists to prevent.
    """
    table: Dict[str, List[Dict[str, Any]]] = {}
    for key, value in (spec_map or {}).items():
        if isinstance(value, list):
            table[str(key)] = [v for v in value if isinstance(v, (str, dict))]

    def _resolve(card: Dict[str, Any]) -> List[Dict[str, Any]]:
        own = default_spec_resolver(card)
        if not table:
            return own
        card_id = str((card or {}).get("quest_id") or (card or {}).get("id") or "")
        extra = _normalize_specs(table.get(card_id) or [])
        named = {str(s.get("source")) for s in own}
        return own + [s for s in extra if str(s.get("source")) not in named]

    return _resolve


class UpdateEngine:
    """The one object a caller asks "what has changed since I last looked at this card".

    Wiring, in full::

        engine = UpdateEngine(client, watermarks=Watermarks(path),
                              drive_comments=DriveComments(token_provider=...))
        bundle = engine.collect(quest, card_id=quest_id)
        text = bundle.as_prompt_block()
        ...   # hand `text` to the run
        bundle.mark_seen()

    ``always`` names sources every card gets whether or not it asked (the person's reflections are
    not a per-card opt-in: they are about the whole of their work). Everything else comes from the
    card's own specs, so two quests can watch entirely different things with no code between them.
    """

    def __init__(self, client: Any = None, *,
                 watermarks: Optional[Watermarks] = None,
                 drive_comments: Any = None,
                 inbound_mail: Any = None,
                 notion: Any = None,
                 google_chat: Any = None,
                 sources: Optional[Sequence[ContextSource]] = None,
                 always: Sequence[str] = DEFAULT_ALWAYS,
                 spec_resolver: Optional[Callable[[Dict[str, Any]], List[Dict[str, Any]]]] = None,
                 relevance_judge: Optional[Callable[..., Optional[set]]] = None,
                 admission_judge: Optional[Callable[[Sequence[ContextUpdate]],
                                                    Optional[set]]] = None,
                 ledger: Any = None,
                 relay_log: Any = None,
                 first_look_days: int = FIRST_LOOK_DAYS,
                 max_updates: int = MAX_UPDATES,
                 now_fn: Optional[Callable[[], datetime]] = None) -> None:
        self._client = client
        self._watermarks = watermarks or Watermarks(None)
        self._spec_resolver = spec_resolver or default_spec_resolver
        self._relevance_judge = relevance_judge
        self._admission_judge = admission_judge
        self._ledger = ledger
        self._relay_log = relay_log
        self._always = tuple(always or ())
        self._first_look = timedelta(days=max(1, int(first_look_days)))
        self._max_updates = max(1, int(max_updates))
        self._now_fn = now_fn or _utcnow
        registry: Dict[str, ContextSource] = {}
        for src in (sources or self._builtin_sources(drive_comments, inbound_mail,
                                                        notion, google_chat)):
            registry[src.name] = src
        self._sources = registry
        # Cache for user-scoped sources (reflections, insights), keyed by each source's own choice
        # of key, cleared once it is older than ``CACHE_TTL_SECONDS`` -- see ``collect``.
        self._cache: Dict[str, Any] = {}
        self._cache_filled_at: Optional[datetime] = None

    @staticmethod
    def _builtin_sources(drive_comments: Any, inbound_mail: Any = None,
                         notion: Any = None, google_chat: Any = None) -> List[ContextSource]:
        return [
            ReflectionsSource(),
            InsightsSource(),
            QuestNotesSource(),
            GoalUpdatesSource(),
            QuestEventsSource(),
            DecisionsSource(),
            CollectionEntriesSource(),
            DriveCommentsSource(drive_comments),
            DriveChangesSource(drive_comments),
            InboundMailSource(inbound_mail),
            NotionDatabaseSource(notion),
            GoogleChatSource(google_chat),
        ]

    def describe_sources(self) -> Dict[str, str]:
        """``{name: one-line description}`` -- the vocabulary a spec can use.

        Exposed so a consumer (or an assistant writing a spec for a card) can discover what is
        available instead of guessing a name that silently does nothing.
        """
        return {name: getattr(src, "describes", "") for name, src in sorted(self._sources.items())}

    def register(self, source: ContextSource) -> None:
        """Add a consumer's own source. The whole extension point: a deployment with a channel
        this library has never heard of implements ``collect`` and registers it, and every caller
        of the engine sees it without any of them changing."""
        self._sources[source.name] = source

    def specs_for(self, card: Dict[str, Any]) -> List[Dict[str, Any]]:
        """This card's source specs: the always-on ones, then whatever the card declares.

        A card declaring a source that is also always-on wins, so ``{"source": "insights",
        "categories": [...]}`` refines the default rather than running twice.
        """
        declared = self._spec_resolver(card) or []
        named = {str(s.get("source")) for s in declared}
        specs = [{"source": name} for name in self._always if name not in named]
        specs.extend(declared)
        return specs


    def _apply_relevance(self, bundle: ContextUpdates, card: Dict[str, Any]) -> None:
        """Drop the user-scoped captures that do not bear on this card, before a run ever sees them.

        Only sources that asked to be judged (``judge_relevance``) take part: a note on this quest
        and a comment on this card's document are relevant because of where they were written, and
        putting those to a judgment could only ever lose one.

        Every failure mode keeps everything. A judge returning None (no provider, a timeout,
        unparsable JSON) leaves the bundle exactly as collected, so switching this on can make a
        brief noisier but never emptier.
        """
        if self._relevance_judge is None:
            return
        judged = [u for u in bundle.updates
                  if getattr(self._sources.get(u.source), "judge_relevance", False)]
        if not judged:
            return
        work = _describe_work(card, bundle.card_label)
        try:
            keep = self._relevance_judge(work, "", judged)
        except Exception as e:  # noqa: BLE001 -- a broken judge never costs the person context
            log.info("context updates: relevance judge failed (%s); keeping everything", e)
            return
        if keep is None:
            return
        # A judged row is kept under its item id, or under its 1-based position for a row with none.
        position = {id(u): f"#{i}" for i, u in enumerate(judged, 1)}
        dropped_by_source: Dict[str, int] = {}
        kept: List[ContextUpdate] = []
        for u in bundle.updates:
            if id(u) in position and (u.item_id or "") not in keep and position[id(u)] not in keep:
                dropped_by_source[u.source] = dropped_by_source.get(u.source, 0) + 1
                continue
            kept.append(u)
        bundle.updates = kept
        for report in bundle.reports:
            if report.source in dropped_by_source:
                report.set_aside = dropped_by_source[report.source]
                report.found = max(0, report.found - report.set_aside)

    def _apply_admission(self, bundle: ContextUpdates, card: Dict[str, Any]) -> None:
        """Drop items that failed "is a real person writing to us", before anything else sees them.

        A SEPARATE gate from ``_apply_relevance``, over a SEPARATE class attribute
        (``judge_admission``), and never folded into it: relevance asks whether an item bears on
        THIS card, admission asks whether anybody is home at all, independent of any card, and a
        source can want one, the other, both, or neither. Run BEFORE relevance, so an item that is
        not even from a person is never spent on a subject-matter judgment either.

        Every failure keeps everything, for the reason ``llm_admission_judge`` documents: a
        dropped request from a real person is invisible, an admitted one is cheap to dismiss.
        """
        if self._admission_judge is None:
            return
        judged = [u for u in bundle.updates
                  if getattr(self._sources.get(u.source), "judge_admission", False)]
        if not judged:
            return
        try:
            keep = self._admission_judge(judged)
        except Exception as e:  # noqa: BLE001 -- a broken judge never costs a real person's ask
            log.info("context updates: admission judge failed (%s); keeping everything", e)
            return
        if keep is None:
            return
        position = {id(u): f"#{i}" for i, u in enumerate(judged, 1)}
        dropped_by_source: Dict[str, int] = {}
        kept: List[ContextUpdate] = []
        for u in bundle.updates:
            if id(u) in position and (u.item_id or "") not in keep and position[id(u)] not in keep:
                dropped_by_source[u.source] = dropped_by_source.get(u.source, 0) + 1
                continue
            kept.append(u)
        bundle.updates = kept
        for report in bundle.reports:
            if report.source in dropped_by_source:
                report.set_aside = report.set_aside + dropped_by_source[report.source]
                report.found = max(0, report.found - dropped_by_source[report.source])

    def _add_owed(self, bundle: ContextUpdates, card_id: str,
                  card: Optional[Dict[str, Any]] = None) -> None:
        """Bring in what is still owed from before, as refs alongside today's news.

        These are not updates in the "something arrived" sense, and they are deliberately carried
        in the same list anyway: a ref is the only handle a run has for accounting for something,
        so an owed item outside the refs is an owed item nothing can ever close. They are marked
        as what they are, and a source's own fresh copy of the same item always wins (an item is
        offered once, with today's wording, not twice).
        """
        if self._ledger is None or not card_id:
            return
        try:
            owed = self._ledger.open_items(card_id)
        except Exception as e:  # noqa: BLE001 -- the ledger never breaks a collection
            log.warning("context updates: could not read the ledger for %s (%s)", card_id, e)
            return
        already = {(u.source, u.item_id) for u in bundle.updates if u.item_id}
        for item in owed:
            if (item.source, item.item_id) in already:
                continue
            if not str(item.text or "").strip():
                continue
            bundle.updates.append(ContextUpdate(
                source=item.source,
                kind="still owed",
                item_id=item.item_id,
                title=(f"{item.author or 'The person'} asked for this on "
                       f"{(item.occurred_at or item.first_seen_at)[:10] or 'an earlier day'}; "
                       f"{item.status_line()}"),
                body=item.text,
                verbatim=True,  # the ask they wrote, re-offered
                excerpt=item.text,
                author=item.author,
                occurred_at=_as_utc(item.occurred_at),
                location=item.location,
                url=item.url,
                how_to_respond=_reply_channel(card or {}),
                needs_response=True,
                raw={"tracked": True, "state": item.state, "kind": item.kind},
            ))

    def collect(self, card: Dict[str, Any], *, card_id: str = "", card_kind: str = "quest",
                since: Optional[datetime] = None,
                card_label: str = "",
                sources: Optional[Sequence[str]] = None,
                options: Optional[Dict[str, Dict[str, Any]]] = None) -> ContextUpdates:
        """Run every source this card watches and return one bundle.

        ``since`` overrides the stored watermark for EVERY source, which is what a caller with its
        own notion of "last time" (an autopilot pass holding ``last_pass_at``) passes in. Left
        None, each source uses its own stored watermark, falling back to the bounded first-look
        window for a card nothing has ever read.

        ``options`` is ``{source_name: {spec key: value}}`` merged over each matching spec for this
        call only: what the CALLER knows about this read that the card does not carry, such as
        which reflection periods fit the scope it is composing for.

        ``sources`` narrows the run to the named channels, leaving the card's own specs otherwise
        untouched. For somebody inspecting one channel ("what is actually coming back from Drive
        for this quest?"), which is a question the whole bundle answers slowly and one source
        answers immediately. A name the card does not watch simply contributes nothing.

        One source failing never costs the others: it is reported and the pass continues.
        """
        now = self._now_fn()
        if (self._cache_filled_at is None
                or (now - self._cache_filled_at).total_seconds() > CACHE_TTL_SECONDS):
            self._cache.clear()
            self._cache_filled_at = now
        cid = str(card_id or card.get("quest_id") or card.get("id") or "")
        bundle = ContextUpdates(
            card_id=cid,
            # _card_label, never the outcome: an outcome is a sentence about the future, and it
            # ended up as the label on every note row of a live receipt.
            card_label=card_label or _card_label(card),
            collected_at=now,
        )
        bundle.card_kind = card_kind
        bundle._watermarks = self._watermarks
        bundle._ledger = self._ledger
        bundle._card = card or {}
        bundle._ask_sources = frozenset(
            name for name, src in self._sources.items() if getattr(src, "tracks_asks", False))
        bundle.relay_client = self._client
        bundle.relay_log = self._relay_log
        bundle.relay_sources = frozenset(
            name for name, src in self._sources.items() if getattr(src, "relay_to_quest", False))
        wanted = {str(n) for n in sources} if sources else None
        for spec in self.specs_for(card):
            name = str(spec.get("source") or "")
            if wanted is not None and name not in wanted:
                continue
            if options and isinstance(options.get(name), dict):
                spec = {**spec, **options[name]}
            source = self._sources.get(name)
            if source is None:
                bundle.reports.append(SourceReport(
                    source=name, spec=spec, error="no such source is registered"))
                log.warning("context updates: card %s asks for unknown source %r", cid, name)
                continue
            last_look = since or self._watermarks.get(cid, name)
            window = last_look or (now - self._first_look)
            report = SourceReport(source=name, spec=spec, since=window)
            request = CollectRequest(
                card=card, card_id=cid, card_kind=card_kind, spec=spec,
                card_label=bundle.card_label, since=window, first_look=last_look is None,
                now=now, client=self._client, cache=self._cache, ledger=self._ledger,
                snapshot_reader=(lambda key, n=name: self._watermarks.get_snapshot(cid, n, key)))
            try:
                found = list(source.collect(request) or [])
            except SourceGap as e:
                report.error = str(e)
                log.info("context updates: source %s has nothing to read for card %s: %s",
                         name, cid, e)
                bundle.reports.append(report)
                continue
            except Exception as e:  # noqa: BLE001 -- one channel never breaks the rest
                report.error = f"{type(e).__name__}: {e}"
                log.warning("context updates: source %s failed for card %s: %s", name, cid, e)
                bundle.reports.append(report)
                continue
            report.found = len(found)
            report.considered = request.considered
            report.explanation = request.explanation
            report.snapshots = dict(request.remembered)
            bundle.reports.append(report)
            bundle.updates.extend(found)

        self._apply_admission(bundle, card)
        self._apply_relevance(bundle, card)
        self._add_owed(bundle, cid, card)

        # Newest first, undated last: an undated row is almost always a standing item (a
        # reflection, an open comment with no timestamp), and it should not displace today's news.
        bundle.updates.sort(
            key=lambda u: u.occurred_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True)
        # Things waiting on an answer are never the ones dropped by the cap.
        if len(bundle.updates) > self._max_updates:
            keep = [u for u in bundle.updates if u.needs_response][:self._max_updates]
            for u in bundle.updates:
                if len(keep) >= self._max_updates:
                    break
                if u not in keep:
                    keep.append(u)
            keep.sort(key=lambda u: u.occurred_at or datetime.min.replace(tzinfo=timezone.utc),
                      reverse=True)
            bundle.updates = keep
        for i, update in enumerate(bundle.updates, 1):
            update.ref = f"U{i}"
        return bundle


# ---------------------------------------------------------------------------------------------
# The consumer-facing factory
# ---------------------------------------------------------------------------------------------

def watermark_path_for(configured: Optional[str] = None,
                       state_path: Optional[str] = None) -> Optional[str]:
    """Where this deployment's watermarks live: what it configured, else beside its state file.

    Deriving a default from the state file matters more than it looks. Without a path the stamps
    live in memory for one process, so a restart re-offers everything inside the first-look window
    and the person reads their own note back to a run that already answered it. Beside the state
    file is where this library already keeps per-deployment bookkeeping, so the default persists
    without anybody configuring a second path.
    """
    if configured:
        return configured
    if not state_path:
        return None
    p = Path(state_path)
    return str(p.with_name(p.stem + "_context_watermarks.json"))


def build_update_engine(cfg: Any = None, client: Any = None, *,
                        state_path: Optional[str] = None,
                        read_only: bool = False) -> Optional[UpdateEngine]:
    """The engine a consumer's config asks for, or None when it asked for none.

    Duck-typed on purpose (``getattr`` with defaults, no ``RunnerConfig`` import): this module is
    about context channels, and a consumer with its own config object, or a test with a stub,
    should be able to build an engine without this file knowing what a ``RunnerConfig`` is.

    Returns None -- not an inert engine -- when ``context_updates`` is off, because every caller
    already treats a missing engine as "compose exactly as before", and an engine that collects
    nothing would still cost a pass through every source.

    ``read_only`` builds the engine on a watermark store that cannot be written (see
    ``Watermarks``). An engine that is only ever asked what a card's context IS -- an inspection,
    a preview, a report -- takes it, and is then safe to run as many times as anyone likes.
    """
    if cfg is not None and not getattr(cfg, "context_updates", True):
        return None
    path = watermark_path_for(getattr(cfg, "context_updates_state_path", None), state_path)
    relay_path = relay_log_path_for(
        getattr(cfg, "context_updates_relay_log_path", None), state_path)
    from .feedback_ledger import build_ledger
    return UpdateEngine(
        client,
        watermarks=Watermarks(path, read_only=read_only),
        ledger=build_ledger(cfg, state_path=state_path, read_only=read_only),
        relay_log=RelayedItems(relay_path, read_only=read_only),
        drive_comments=getattr(cfg, "drive_comments", None),
        inbound_mail=getattr(cfg, "inbound_mail", None),
        notion=getattr(cfg, "notion_adapter", None),
        google_chat=getattr(cfg, "google_chat_adapter", None),
        spec_resolver=consumer_spec_resolver(getattr(cfg, "context_sources_map", None)),
        # A callable, not a provider: this engine is built before the CLI wraps
        # cfg.model_provider with MultiProvider, so resolving it at CALL time is what makes the
        # judge use the routed provider instead of a raw one that 404s on half the model ids.
        relevance_judge=(llm_relevance_judge(
            lambda: getattr(cfg, "model_provider", None),
            tier=str(getattr(cfg, "context_updates_relevance_tier", "balanced") or "balanced"))
            if (cfg is not None and getattr(cfg, "context_updates_judge_relevance", True))
            else None),
        # Same callable-not-provider reasoning as relevance_judge, and the same "on unless a
        # consumer turned it off" default: without it, whatever cleared the deterministic header
        # check in adapters.inbound_mail (or any other judge_admission source) reaches a run and
        # the account's own asks record with no further filtering at all.
        admission_judge=(llm_admission_judge(
            lambda: getattr(cfg, "model_provider", None),
            tier=str(getattr(cfg, "context_updates_admission_tier", "fast") or "fast"))
            if (cfg is not None and getattr(cfg, "context_updates_judge_admission", True))
            else None),
        first_look_days=int(getattr(cfg, "context_updates_first_look_days", FIRST_LOOK_DAYS) or
                            FIRST_LOOK_DAYS),
    )


def collect_quest_context(quest_id: str, *, cfg: Any = None, client: Any = None,
                          since: Optional[datetime] = None, days: Optional[float] = None,
                          sources: Optional[Sequence[str]] = None,
                          options: Optional[Dict[str, Dict[str, Any]]] = None,
                          card_label: str = "",
                          state_path: Optional[str] = None) -> ContextUpdates:
    """**What is the context for this quest right now?** One call, a read, repeatable.

    The whole of it::

        bundle = collect_quest_context("quest_1625d9f47a06", cfg=load_config("qar.toml"))
        print(bundle.as_prompt_block())      # exactly what a run on this quest would be handed

    This exists because, before it, the only ways to see a quest's context were to run the thing
    that consumes it (an autopilot pass, an executor task) or to rebuild the engine's wiring by
    hand. The first has side effects and a cadence gate, and the second is a copy of library code
    living outside the library, drifting from it. Neither is a thing to hand somebody who just
    wants to look. Looking is a first-class operation, so it is one function.

    It is a READ, by construction rather than by care: the engine is built on a read-only watermark
    store (see ``Watermarks``), so nothing here can mark anybody's comment as seen, and running it
    ten times in a row is the same as running it once. Automatic delivery is unaffected -- the next
    real run still offers everything it would have offered.

    Parameters, all optional but the quest:

    - ``cfg``     a ``RunnerConfig`` (``quest_ai_runner.load_config``). Supplies the credentials,
                  the watermark file, the quest -> context-sources map and the Drive client, so the
                  read sees exactly what the deployment's own runs see.
    - ``client``  an already-built ``QuestClient``, when the caller has one. Built from ``cfg``
                  otherwise.
    - ``since`` / ``days``   the time period: look back from this moment, instead of from the
                  stored "an assistant last looked" stamp. ``days=7`` is ``since=now - 7 days``.
                  Left out, each source uses its own watermark, which is what a run gets.
    - ``sources`` only these channels ("drive_comments", "insights", ...), out of the ones the
                  quest watches. ``UpdateEngine.describe_sources()`` lists the vocabulary.
    - ``options`` ``{source: {spec key: value}}``, merged over the quest's own spec for this call
                  (e.g. ``{"reflections": {"periods": ("week", "month")}}``).
    - ``card_label``  how the quest is named in each row. Left out, the engine labels it the way it
                  labels every card (its name, never its outcome). A caller reproducing a
                  particular run's text passes that run's own label.
    - ``state_path``  the lane's state file, when it is not ``cfg.state_path``; the watermarks sit
                  beside it. Only read, never written.
    """
    if since is None and days is not None:
        since = _utcnow() - timedelta(days=float(days))
    if client is None:
        if cfg is None:
            raise ValueError("collect_quest_context needs a client, or a RunnerConfig to build "
                             "one from (quest_ai_runner.load_config)")
        from .quest_client import QuestClient   # local: this module is duck-typed on its client
        client = QuestClient(cfg.quest_base_url, cfg.quest_api_key, team_id=cfg.team_id)
    engine = build_update_engine(cfg, client,
                                 state_path=state_path or getattr(cfg, "state_path", None),
                                 read_only=True)
    if engine is None:
        raise ValueError("context updates are switched off for this config "
                         "(RunnerConfig.context_updates / QAR_CONTEXT_UPDATES), so there is no "
                         "engine to ask")
    quest = client.get_quest(quest_id)
    return engine.collect(quest, card_id=quest_id, card_kind="quest", card_label=card_label,
                          since=since, sources=sources, options=options)
