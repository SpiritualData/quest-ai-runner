"""Executing the planner's read specs and direct tool calls.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional
from concurrent.futures import TimeoutError as FuturesTimeoutError

from hubofhubs.ports.scope import ScopedThreadPoolExecutor as ThreadPoolExecutor
from .adapters_events import EVENT_CONTEXT, EVENT_EXEC
from .adapters_values import Observation, ProgressEvent
from .guard import ExecutionFact, ExecutionRecord
from .tools import ToolContext
from .planner_context import _is_discovery_spec
from .orchestrator_events import _project_card_metadata_for_event, _project_sources_for_event
from .orchestrator_constants import log
from .orchestrator_turn import TurnCardCache, _Emitter
from .orchestrator_capabilities import describe_read_spec, read_op_timeout_seconds


# A tool that acts (sends, writes) echoes its arguments to the re-plan and the answer check this
# long: the message it sent IS the evidence, and a body cut at 600 characters made the check say it
# could not see the closing line of an email that had gone out whole.
ACTING_TOOL_ARGS_CHARS = 4000


class ReadsMixin:
    """Executing the planner's read specs and direct tool calls."""

    # --- gather (parallel reads/greps/queries via the RetrievalAdapter) ------

    def _exec_one_read(self, spec: Dict[str, Any],
                       guidance_selected_ids: Optional[set] = None,
                       card_context: Optional["TurnCardCache"] = None) -> Optional[Observation]:
        if not isinstance(spec, dict):
            return None
        # CARD CONTEXT (the unified primitive, mid-loop): reach the SAME cards + assembly the
        # turn-start path uses, at any loop step. Handled BEFORE the retrieval-None guard because
        # these route through the ContextAssembler, not the RetrievalAdapter (see the two helpers).
        if spec.get("cards") is not None:
            return self.read_cards_context(str(spec["cards"]), card_context)
        if spec.get("card") is not None:
            return self.read_one_card(str(spec["card"]), card_context)
        # TOOL CATALOG SEARCH: the planner is shown only the tools relevant to the request; this
        # read searches the whole catalog when it needs one that was not shown.
        if spec.get("tools") is not None and self.tools is not None:
            query = str(spec["tools"])
            found = self.tools.search(query, k=8)
            text = (self.tools.render_catalog(found) if found
                    else f"No tool matches {query!r}. Available: {', '.join(self.tools.names())}")
            return Observation(kind="query", locator=f"tools({query})",
                               text="TOOLS MATCHING YOUR SEARCH (call with action \"tool\"):\n"
                                    + text)
        # LIVE WEB (the WebResearch adapter, core/adapters_retrieval.py): dispatched directly here, BEFORE
        # the retrieval-None guard below, because this never goes through ``self.retrieval`` --
        # folding it into CompositeRetrievalAdapter would broadcast every grep/query to it too,
        # firing a paid, slow web search on an ordinary corpus read. ``self.web is None`` returns
        # a NAMED error Observation (never raises, never silently drops the spec) so the planner
        # learns web search is unavailable here and moves on instead of retrying the same spec.
        if spec.get("web") is not None:
            if self.web is None:
                return Observation(kind="error",
                                    error="Web search is not configured in this deployment.")
            queries = spec["web"]
            try:
                return self.web.search(queries, fresh=bool(spec.get("fresh")))
            except Exception as e:  # noqa: BLE001 — a web adapter must never break the loop
                return Observation(kind="error", error=f"web search failed: {e}")
        if spec.get("web_page"):
            if self.web is None:
                return Observation(kind="error",
                                    error="Web search is not configured in this deployment.")
            try:
                page_kwargs: Dict[str, Any] = {}
                # Whole-document paging is an optional extension of the web adapter: only pass it
                # when the planner asked, so an adapter without it keeps working unchanged.
                if spec.get("full") or spec.get("part"):
                    page_kwargs["full"] = bool(spec.get("full"))
                    if spec.get("part"):
                        page_kwargs["part"] = spec.get("part")
                return self.web.fetch(str(spec["web_page"]), focus=spec.get("focus") or None,
                                      fresh=bool(spec.get("fresh")), **page_kwargs)
            except Exception as e:  # noqa: BLE001 — a web adapter must never break the loop
                return Observation(kind="error", error=f"web page fetch failed: {e}")
        # No retrieval adapter: gracefully report unsupported rather than crashing. The brain
        # can still answer from transcript/context_view; it just cannot ground on a corpus.
        if self.retrieval is None and not (
            spec.get("list_guidance") or spec.get("read_guidance")
        ):
            return Observation(kind="query", locator="corpus",
                               text="No retrieval adapter configured — corpus grounding unavailable.")
        try:
            # GUIDANCE discovery (the GuidanceProvider role) — dispatched via getattr/None-guard so
            # an orchestrator with guidance=None returns a benign Observation, never raises. Both
            # flow into ``gathered`` as Observation(kind="query"), the SAME path as any read.
            if spec.get("list_guidance"):
                if self.guidance is None:
                    return Observation(kind="query", locator="list_guidance",
                                       text="No guidance is available for this assistant.")
                try:
                    cards = self.guidance.list() or []
                except Exception:  # noqa: BLE001 — a provider must never break the loop
                    cards = []
                if not cards:
                    return Observation(kind="query", locator="list_guidance",
                                       text="No guidance cards are available.")
                lines = [f"- {c.id}: {c.title} — applies when: {c.relevance}" for c in cards]
                return Observation(kind="query", locator="list_guidance",
                                   text="AVAILABLE GUIDANCE (read_guidance by id for the full "
                                        "instructions):\n" + "\n".join(lines))
            if spec.get("read_guidance"):
                card_id = str(spec["read_guidance"])
                if self.guidance is None:
                    return Observation(kind="query", locator=f"read_guidance({card_id})",
                                       text="No guidance is available for this assistant.")
                # De-dupe: a card already pre-selected into APPLICABLE GUIDANCE this turn is
                # already in front of the model — point back to it instead of re-injecting it.
                if guidance_selected_ids and card_id in guidance_selected_ids:
                    return Observation(
                        kind="query", locator=f"read_guidance({card_id})",
                        text=f"Guidance {card_id!r} was already provided above under "
                             "APPLICABLE GUIDANCE; refer to it there.")
                try:
                    card = self.guidance.read(card_id)
                except Exception:  # noqa: BLE001
                    card = None
                if card is None:
                    return Observation(kind="query", locator=f"read_guidance({card_id})",
                                       text=f"No guidance card with id {card_id!r}.")
                return Observation(
                    kind="query", locator=f"read_guidance({card_id})",
                    text=f"GUIDANCE: {card.title}\n(applies when: {card.relevance})\n\n{card.body}")
            # Discovery specs first — dispatched via getattr so a structural adapter that
            # predates the discovery methods degrades to a benign "unsupported" Observation
            # instead of raising (back-compat: the four methods are optional on the Protocol).
            if spec.get("list_sources"):
                fn = getattr(self.retrieval, "list_sources", None)
                return fn() if fn else Observation(
                    kind="query", locator="list_sources",
                    text="discovery not supported by this adapter")
            if spec.get("describe_source"):
                name = str(spec["describe_source"])
                fn = getattr(self.retrieval, "describe_source", None)
                return fn(name, path=spec.get("describe_path") or None) if fn else Observation(
                    kind="query", locator=f"describe_source({name})",
                    text="discovery not supported by this adapter")
            if spec.get("list_operations"):
                fn = getattr(self.retrieval, "list_operations", None)
                return fn() if fn else Observation(
                    kind="query", locator="list_operations",
                    text="discovery not supported by this adapter")
            if spec.get("describe_operation"):
                name = str(spec["describe_operation"])
                fn = getattr(self.retrieval, "describe_operation", None)
                return fn(name) if fn else Observation(
                    kind="query", locator=f"describe_operation({name})",
                    text="discovery not supported by this adapter")
            if spec.get("grep"):
                # A spec that names a file AND a search (the planner contract allows "and/or") is
                # about that file: read it first, because a guessed `scope` makes the search half
                # wander or miss while the named file answers directly. If the file cannot be read,
                # the search runs; if both fail, the read's error (which may name the closest real
                # paths) is the one reported.
                file_error = None
                if spec.get("rel_path"):
                    got = self.retrieval.read_section(
                        str(spec["rel_path"]), start_line=spec.get("start_line"),
                        end_line=spec.get("end_line"), heading=spec.get("heading"),
                        max_bytes=self.cfg.max_gather_chars)
                    if got is not None and got.kind != "error":
                        return got
                    file_error = got
                found = self.retrieval.grep(
                    str(spec["grep"]), scope=spec.get("scope") or None
                )
                if found is not None and found.kind == "error" and file_error is not None:
                    return file_error
                return found
            if spec.get("query") is not None:
                # Planner specs nest the query params under "query" ({"query": {"text": ...},
                # "time_range": ...}), but adapters read params at the TOP level of the dict
                # they receive (text/collection/...). Flatten the nested params over the spec
                # (nested keys win) so both shapes reach every adapter; sibling constraint keys
                # (time_range, topic_terms, actor, content_kind) stay intact. Without this, an
                # adapter expecting top-level "text" saw only the nested dict and the planner's
                # semantic-search step silently returned an error observation every time.
                payload = dict(spec)
                if isinstance(spec["query"], dict):
                    payload.update(spec["query"])
                return self.retrieval.query(payload)
            if spec.get("rel_path"):
                return self.retrieval.read_section(
                    str(spec["rel_path"]),
                    start_line=spec.get("start_line"),
                    end_line=spec.get("end_line"),
                    heading=spec.get("heading"),
                    max_bytes=self.cfg.max_gather_chars,
                )
        except Exception as e:  # noqa: BLE001 — a bad spec must never break the loop
            log.warning(f"Read spec execution failed: {type(e).__name__}: {e}", exc_info=True)
            return Observation(kind="error", error=type(e).__name__)
        return None

    # --- CARD CONTEXT reads: the unified primitive reachable at any loop step ------------------

    def read_cards_context(self, query: str,
                           card_context: Optional["TurnCardCache"]) -> Observation:
        """Run card/topic context assembly for ``query`` mid-loop, through the SAME assembler the
        turn-start path uses. Serves from the shared in-run cache (incl. a late-landing turn-start
        pre-fetch) when possible, else runs a fresh bounded assemble. A timeout or failure returns a
        NAMED error observation (never empty), matching the WS1 discipline. Never raises."""
        query = (query or "").strip()
        if not query:
            return Observation(kind="query", locator="cards",
                               text="No query text was given for the cards read.")
        if card_context is None or card_context.assembler is None:
            return Observation(
                kind="query", locator=f"cards({query!r})",
                text="No context assembler is configured, so topic/card context is unavailable.")
        timeout = read_op_timeout_seconds()
        try:
            assembled, origin = card_context.assemble_for_query(query, timeout)
        except FuturesTimeoutError:
            return Observation(
                kind="error", locator=f"cards({query!r})",
                error=f"Card context assembly for {query!r} timed out after {timeout:.0f}s")
        except Exception as e:  # noqa: BLE001 — a mid-loop read must never break the loop
            log.warning(f"Mid-loop cards read failed: {type(e).__name__}: {e}", exc_info=True)
            return Observation(kind="error", locator=f"cards({query!r})",
                               error=f"{type(e).__name__}: {e}")
        text = (getattr(assembled, "context_view", "") or "").strip() if assembled is not None else ""
        card_meta = list(getattr(assembled, "card_metadata", None) or []) if assembled is not None else []
        sources = list(getattr(assembled, "sources", None) or []) if assembled is not None else []
        self.emit_context_event_midloop(card_context, query, card_meta, sources, origin)
        if not text:
            return Observation(
                kind="query", locator=f"cards({query!r})",
                text=f"No topic/card context found for {query!r} "
                     f"(assembly ran via {origin}, found nothing).")
        return Observation(kind="query", locator=f"cards({query!r})", text=text)

    def read_one_card(self, card_id: str,
                      card_context: Optional["TurnCardCache"]) -> Observation:
        """Fetch ONE card's rendered content by id mid-loop, via the assembler's optional
        ``render_card``. Unsupported store / absent card / timeout each return a NAMED observation
        (never empty). Never raises."""
        card_id = (card_id or "").strip()
        if not card_id:
            return Observation(kind="query", locator="card",
                               text="No card id was given for the card read.")
        if card_context is None or card_context.assembler is None:
            return Observation(kind="query", locator=f"card({card_id})",
                               text="No context assembler is configured, so card fetch is unavailable.")
        timeout = read_op_timeout_seconds()
        try:
            text, origin = card_context.render_card(card_id, timeout)
        except FuturesTimeoutError:
            return Observation(kind="error", locator=f"card({card_id})",
                               error=f"Fetching card {card_id!r} timed out after {timeout:.0f}s")
        except Exception as e:  # noqa: BLE001 — a mid-loop read must never break the loop
            log.warning(f"Mid-loop card read failed: {type(e).__name__}: {e}", exc_info=True)
            return Observation(kind="error", locator=f"card({card_id})",
                               error=f"{type(e).__name__}: {e}")
        if origin == "unsupported":
            return Observation(
                kind="query", locator=f"card({card_id})",
                text="This assistant's context store does not support fetching a single card by "
                     "id; use a cards read (a query string) to retrieve topic context instead.")
        text = (text or "").strip()
        if not text:
            return Observation(kind="query", locator=f"card({card_id})",
                               text=f"No context card with id {card_id!r}.")
        self.emit_context_event_midloop(card_context, card_id, [{"id": card_id}], [], "card")
        return Observation(kind="query", locator=f"card({card_id})", text=text)

    def emit_context_event_midloop(self, card_context: Optional["TurnCardCache"], query: str,
                                   card_meta: List[Dict[str, Any]],
                                   sources: List[Dict[str, Any]], origin: str) -> None:
        """Emit EVENT_CONTEXT for a mid-loop card read, marked ``midloop`` so a consumer can tell it
        apart from turn-start assembly (docs sec. 3, point 4). Best-effort; never raises."""
        emit = getattr(card_context, "emit", None) if card_context is not None else None
        if emit is None:
            return
        try:
            card_meta_light = _project_card_metadata_for_event(card_meta or [])
            sources_light = _project_sources_for_event(sources or [])
            count = len(card_meta or [])
            text = (f"Fetched {count} context card{'s' if count != 1 else ''} mid-turn."
                    if count else "")
            emit.emit(ProgressEvent(
                type=EVENT_CONTEXT,
                text=text,
                data={
                    "card_metadata": card_meta_light,
                    "sources": sources_light,
                    "card_count": count,
                    "source_count": len(sources or []),
                    # The marker distinguishing a mid-loop card read from turn-start assembly.
                    "midloop": True,
                    "origin": origin,
                    "internal": True,
                }))
        except Exception as e:  # noqa: BLE001
            log.debug(f"Failed to emit mid-loop EVENT_CONTEXT: {e}", exc_info=True)

    def _do_reads(self, reads: List[Dict[str, Any]],
                  guidance_selected_ids: Optional[set] = None,
                  card_context: Optional["TurnCardCache"] = None) -> List[Dict[str, Any]]:
        specs = [s for s in (reads or [])[: self.cfg.max_reads_per_step] if isinstance(s, dict)]
        if not specs:
            return []
        def _tagged(obs: Observation, spec: Dict[str, Any]) -> Dict[str, Any]:
            # Tag discovery/capability listings so the answer path can exclude them (menu, not content).
            d = obs.to_dict()
            if _is_discovery_spec(spec):
                d["discovery"] = True
            return d

        if len(specs) == 1:
            obs = self._exec_one_read(specs[0], guidance_selected_ids, card_context)
            return [_tagged(obs, specs[0])] if obs is not None else []
        workers = min(self.cfg.max_parallel, len(specs))
        op_timeout = read_op_timeout_seconds()
        results: List[Optional[Observation]] = [None] * len(specs)
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {pool.submit(self._exec_one_read, s, guidance_selected_ids, card_context): i
                       for i, s in enumerate(specs)}
            for fut in futures:
                i = futures[fut]
                try:
                    # Per-op timeout (opposite discipline from the untimed version): one slow
                    # adapter member must never wedge every other read in this step. Named
                    # explicitly so a timeout reports "operation X stalled", never an empty
                    # result that looks like "nothing found".
                    results[i] = fut.result(timeout=op_timeout)
                except FuturesTimeoutError:
                    op_name = describe_read_spec(specs[i])
                    # ONE retry before giving up. Motivating incident (2026-09-14, dissertation
                    # brief): a run hit one context-assembly timeout and one read timeout, then
                    # reported "not reachable this run" for content it never actually retried -- the
                    # model has no way to distinguish "genuinely missing" from "this one attempt was
                    # unlucky" from a single named error. CORRECTION, logged here rather than in a
                    # commit message that already shipped: the original commit attributed that
                    # read's stall to a context-index rebuild racing the task's claim. Adversarial
                    # review of the actual timeline found the rebuild finished ~90s before the
                    # context-assembly timeout and a full ~3 minutes before the read timeout
                    # itself fired, and the SAME process hit another context-assembly timeout plus
                    # a guidance-selection timeout later that morning (08:07) with no rebuild
                    # anywhere nearby -- so this is a process that stalls repeatedly for a cause
                    # still unidentified, not a one-off race. What's actually verified: a local file
                    # read stalling for 60s on Path.read_text of a small file is not explained by
                    # disk speed, and is cheap and idempotent to retry regardless of the real cause
                    # -- unlike the `claude` subprocess retries QAR_MAX_PARALLEL's own comment warns
                    # about (those pile on MORE concurrent processes under load; this retries a plain
                    # file read on the same pool). The retry below is a real mitigation for the
                    # symptom that fired once in that incident; it does nothing for a context-
                    # assembly timeout (the one that fired twice) and is not itself a diagnosis of
                    # what blocks the thread. FOLLOW-UP NEEDED: find what actually holds a read
                    # thread for 60s in this process, and decide whether context-assembly timeouts
                    # deserve the same retry or a larger budget. The abandoned first attempt's thread
                    # is left to finish on its own, same as any other timed-out read here --
                    # retrying never waits on it.
                    log.warning(
                        "Read operation timed out: %s exceeded %.0fs, retrying once "
                        "(QAR_READ_OP_TIMEOUT_SECONDS to adjust)", op_name, op_timeout)
                    try:
                        retry_fut = pool.submit(
                            self._exec_one_read, specs[i], guidance_selected_ids, card_context)
                        results[i] = retry_fut.result(timeout=op_timeout)
                        log.info("Read operation %s succeeded on retry", op_name)
                    except FuturesTimeoutError:
                        log.warning(
                            "Read operation timed out again on retry: %s exceeded %.0fs twice "
                            "(QAR_READ_OP_TIMEOUT_SECONDS to adjust)", op_name, op_timeout)
                        results[i] = Observation(
                            kind="error",
                            error=f"Operation '{op_name}' timed out after {op_timeout:.0f}s "
                                  f"(retried once, still timed out)")
                    except Exception as e:  # noqa: BLE001
                        log.warning(f"Read operation failed on retry: {type(e).__name__}: {e}",
                                   exc_info=True)
                        results[i] = Observation(kind="error", error=f"{type(e).__name__}: {e}")
                except Exception as e:  # noqa: BLE001
                    log.warning(f"Read operation failed: {type(e).__name__}: {e}", exc_info=True)
                    results[i] = Observation(kind="error", error=f"{type(e).__name__}: {e}")
        finally:
            # Non-blocking shutdown: every spec's outcome was already reported above (via a
            # normal result or a named timeout), so the turn must not then sit here waiting for
            # a still-running worker thread to actually finish (Python threads cannot be
            # force-killed) -- mirrors the context-assembly executor's own wait=False shutdown,
            # for the same reason.
            pool.shutdown(wait=False)
        return [_tagged(r, specs[i]) for i, r in enumerate(results) if r is not None]

    def run_tool_calls(self, calls: List[Dict[str, Any]], *, gathered: List[Dict[str, Any]],
                        exec_record: ExecutionRecord, tool_ok_signatures: set,
                        emit: "_Emitter", steps: int, ctx: ToolContext,
                        on_success: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> None:
        """Run the planner's direct tool calls in order, in-process (no deep run).

        Each outcome lands twice: in ``gathered`` (so the re-plan and the answer see the receipt)
        and, for a tool that ACTS, in ``exec_record`` (so the claim verifier backs "sent" with a
        real success, and the answer->deep nets see that work already ran this turn). A call
        identical to one that already succeeded is skipped, never repeated.
        """
        for call in calls[: self.cfg.max_reads_per_step]:
            name = call.get("name") or ""
            args = call.get("args") or {}
            spec = self.tools.get(name) if self.tools is not None else None
            sig = (name, json.dumps(args, sort_keys=True, default=str))
            if sig in tool_ok_signatures:
                gathered.append({"kind": "query", "locator": f"tool:{name}",
                                 "text": f"TOOL {name}: ALREADY SUCCEEDED earlier this turn with "
                                         f"these args; not run again. Answer with its receipt."})
                continue
            emit.status(f"Calling {name}…")
            emit.emit(ProgressEvent(type=EVENT_EXEC, step=steps, text=f"tool {name}",
                                    data={"phase": "tool_call", "tool": name}))
            result = (self.tools.invoke(name, args, ctx) if self.tools is not None
                      else None)
            ok = bool(result and result.ok)
            text = result.text if result is not None else "no tools configured"
            if ok:
                tool_ok_signatures.add(sig)
                if on_success is not None:
                    try:
                        on_success(name, dict(args))
                    except Exception:  # noqa: BLE001 -- learning a recipe never breaks a turn
                        pass
            gathered.append({"kind": "query", "locator": f"tool:{name}",
                             "text": f"TOOL {name} {'SUCCEEDED' if ok else 'FAILED'} "
                                     f"(args {json.dumps(args, default=str)[:(ACTING_TOOL_ARGS_CHARS if (spec is None or spec.mutates) else 600)]}):\n{text}"})
            if spec is None or spec.mutates:
                exec_record.facts.append(ExecutionFact(
                    goal=f"tool {name}: {text[:200]}" if ok else f"tool {name}",
                    succeeded=ok, failed=not ok, error=None if ok else text[:300],
                    phases=["tool_call", "done" if ok else "failed"]))
            emit.emit(ProgressEvent(type=EVENT_EXEC, step=steps,
                                    text=f"tool {name}: {text[:200]}",
                                    data={"phase": "done" if ok else "failed", "tool": name}))
            log.info("tool %s %s: %s", name, "ok" if ok else "FAILED", text[:200])
