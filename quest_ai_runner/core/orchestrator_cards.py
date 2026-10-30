"""Context-card updates after a deep run, and the anticipation kickoff.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

from .auxiliary_prompts import CARD_UPDATE_NOTHING_LEARNABLE, CARD_UPDATE_PROMPT, CARD_UPDATE_TOOL
from .card_learning import (
    OBSERVATIONS_HEADING,
    narrow_edits_to_removals,
    render_observations_block,
    turn_observations,
    turn_teaches_new_facts,
)
from hubofhubs.ports.model import STEP_SUMMARIZE, answer_with_reasoning, plan_with_step
from .adapters_values import PlanDecision
from .card_filter import _extract_json
from .orchestrator_text import _card_update_store, _deep_future_context
from .orchestrator_constants import DEFAULT_CARD_MERGE_SIMILARITY, log
from .orchestrator_decisions import _normalize_card_edits, _proposed_card_text
from .orchestrator_turn import _Emitter
from .prompt_specs import render_prompt
from .orchestrator_types import OrchestratorResult


class CardsMixin:
    """Context-card updates after a deep run, and the anticipation kickoff."""

    # --- async post-deep context-card updater (prepare for the FUTURE) -------

    def _card_updater_active(self) -> bool:
        """True when the async post-deep card updater can run: the toggle is on, a provider is
        wired, and the wired context_assembler exposes (or wraps) the card-update API. Never raises.

        When False, the deep loop is byte-for-byte unchanged: no future-context instruction is
        appended to deep briefs, and no updater LLM call is made.
        """
        try:
            return bool(self.cfg.async_card_update
                        and self.provider is not None
                        and _card_update_store(self.context_assembler) is not None)
        except Exception:  # noqa: BLE001
            return False

    def _select_current_cards(self, query: str,
                              ctx_meta: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Select the user's CURRENT relevant cards for ``query`` via the wired assembler, so the
        updater can CORRECT existing ones rather than only add. Returns the assembler's
        ``card_metadata`` list (``[{id, title, files, ...}]``) or []. Never raises."""
        if self.context_assembler is None or not (query or "").strip():
            return []
        try:
            assembled = self.context_assembler.assemble(query, meta=ctx_meta or None)
            return list(getattr(assembled, "card_metadata", None) or [])
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _scoped_card_id(card_id: str, user_id: Optional[str]) -> str:
        """Attribute a card id to a user so cards never leak across users. Existing ids that already
        carry the user prefix (an UPDATE of a current card) are left as-is; a fresh slug is prefixed
        with ``u:<user_id>:`` so a created card is user-scoped. No user_id means no change (single-
        tenant / unscoped deployments behave as before). Never raises."""
        cid = (card_id or "").strip()
        if not user_id:
            return cid
        prefix = f"u:{user_id}:"
        if not cid:
            return prefix
        return cid if cid.startswith(prefix) else prefix + cid

    def _update_cards_after_deep(
        self,
        *,
        request: str,
        executed: str,
        future_context: str,
        ctx_meta: Optional[Dict[str, Any]],
        observations: Optional[List[str]] = None,
        teaches_new_facts: bool = True,
    ) -> int:
        """SYNC post-deep card updater (the loop also runs this in a background thread).

        Makes ONE cheap LLM call that turns the finished run into a STRUCTURED set of card edits
        (fields name/description + content add/replace/remove), then applies them via the card-update
        API, user-scoped by ``ctx_meta['user_id']`` and bounded by the config caps. Returns the number
        of cards it successfully wrote (0 when inactive, nothing to do, or a parse miss). Best-effort:
        NEVER raises and NEVER affects the OrchestratorResult. Exposed as a sync method so a test can
        call it directly; the loop invokes it off the result path in a thread.

        ``observations`` is WHAT THE TURN OBSERVED (``core/card_learning.py``): the deep runs' own
        write receipts and the reads that returned content. It is the material a new card fact may
        come from, and it is handed to the model as such. ``teaches_new_facts`` is the structural
        verdict on whether this turn may record anything new at all; when False the edit plan is
        NARROWED to its removals after the call, so a turn that landed no change, did not verify
        what it did, or observed nothing cannot write a fact onto a durable card no matter what the
        model returns.
        """
        try:
            store = _card_update_store(self.context_assembler)
            if store is None or self.provider is None or not self.cfg.async_card_update:
                return 0
            user_id = (ctx_meta or {}).get("user_id")
            current_cards = self._select_current_cards(request, ctx_meta)
            if not teaches_new_facts and not current_cards:
                # Nothing new may be recorded and there is no existing card to correct, so the only
                # edits that could survive the narrowing do not exist. Skip the call entirely.
                log.debug("post-deep card update skipped: this turn teaches nothing and there is "
                          "no current card to correct")
                return 0
            current_view = self._render_cards_for_updater(current_cards)
            # The user-scoped ids of the cards we SHOWED the updater (its CURRENT CARDS). These are
            # exactly the ids it can reference for an UPDATE; any other id is a would-be CREATE and is
            # eligible for the semantic-merge redirect below.
            known_ids = {
                self._scoped_card_id(str(c.get("id")), user_id)
                for c in current_cards
                if isinstance(c, dict) and c.get("id")
            }

            model = self.registry.resolve_tier(self.cfg.planner_tier)
            prompt = render_prompt("feed-context-sources.card-update", 
                request=(request or "")[:2000],
                executed=(executed or "")[:6000],
                future_context=(future_context or "(none)")[:3000],
                observed_heading=OBSERVATIONS_HEADING,
                observed=render_observations_block(observations),
                learning_limit=("" if teaches_new_facts else CARD_UPDATE_NOTHING_LEARNABLE),
                current_cards=current_view or "(no current cards)",
            )
            # Prefer a forced-tool structured return; degrade to text + _extract_json on a provider
            # that only does plain answers. The model can return an empty set even when the
            # future-context names reusable sources, so retry ONCE on empty (still at most two cheap
            # calls). Any miss after that -> do nothing. A turn that may teach nothing is NOT
            # retried: an empty answer there is the right answer, and coaxing it costs a second call
            # for edits that would be narrowed away.
            edits = self._call_card_updater(prompt, model)
            if not edits and teaches_new_facts:
                edits = self._call_card_updater(prompt, model)
            if not teaches_new_facts:
                # STRUCTURAL GATE: the turn observed nothing it may record, so only removals stand.
                kept = narrow_edits_to_removals(edits)
                if len(kept) != len(edits):
                    log.info("Card learning: this turn may teach nothing new, so %d of %d proposed "
                             "card edit(s) were dropped and only removals kept",
                             len(edits) - len(kept), len(edits))
                edits = kept
            if not edits:
                return 0
            scope_tags = (ctx_meta or {}).get("scope_tags")
            return self._apply_card_edits(
                store, edits, user_id, known_card_ids=known_ids, scope_tags=scope_tags)
        except Exception:  # noqa: BLE001 — the updater must never affect the run
            log.debug("post-deep card update failed", exc_info=True)
            return 0

    def _call_card_updater(self, prompt: str, model: str) -> List[Dict[str, Any]]:
        """Make the single updater LLM call and return the parsed ``edits`` list (or []). Never
        raises. Uses forced tool use when the provider supports ``plan``; else parses ``answer``
        text with the repo's ``_extract_json`` helper. A parse miss yields []."""
        raw: Any = None
        try:
            if hasattr(self.provider, "plan"):
                raw = plan_with_step(self.provider, prompt, model=model,
                                     tool_schema=CARD_UPDATE_TOOL, step=STEP_SUMMARIZE)
        except Exception:  # noqa: BLE001 — fall through to the text path
            raw = None
        # plan() may return a dict, a bare list (tool args as an array), or None. Only fall back to
        # the text path when it gave us nothing usable.
        if not isinstance(raw, (dict, list)) or (isinstance(raw, list) and not raw):
            try:
                txt = answer_with_reasoning(self.provider, [{"role": "user", "content": prompt}],
                                            model=model, step=STEP_SUMMARIZE)
            except Exception:  # noqa: BLE001
                return []
            try:
                raw = json.loads(_extract_json(txt or "") or "null")
            except Exception:  # noqa: BLE001 — parse miss: do nothing
                return []
        return _normalize_card_edits(raw)

    def _apply_card_edits(self, store: Any, edits: List[Dict[str, Any]],
                          user_id: Optional[str],
                          known_card_ids: Optional[set] = None,
                          scope_tags: Optional[List[str]] = None) -> int:
        """Apply parsed card edits via the card-update API, user-scoped + bounded. Returns the count
        of cards written. Never raises (each card edit is independently guarded).

        ``scope_tags`` (see ``core/scope_tags.py``), when given, are passed to every
        ``store.update_card`` call so a card this turn touches is UNIONED with the quest(s) this
        turn is scoped to (never overwritten) -- a card learned while working on quest X, then
        touched again from quest Y, becomes visible to both.

        SEMANTIC CARD-MERGE (item 3): for an edit that would CREATE a new card (its user-scoped id is
        NOT one of ``known_card_ids`` -- the cards the updater was actually shown, the only ids it can
        target for an update), first ask the store's OPTIONAL ``find_similar_card`` capability whether
        a sufficiently-similar card already exists in THIS user's scope; if so, REDIRECT the edit to
        UPDATE that card (merging the proposed fields + content) instead of creating a near-duplicate
        twin. The capability is detected by duck-typing (``callable(getattr(store, ...))``), so a card
        store without embeddings (the keyword-only ``FileContextStore``) is skipped entirely and the
        edit creates as before. Disabled when the threshold is >= 1.0. Any miss/error -> create as
        before; an edit that already targets a known existing card id is left untouched.
        """
        written = 0
        max_cards = max(0, self.cfg.async_card_update_max_cards)
        max_edits = max(0, self.cfg.async_card_update_max_edits_per_card)
        known = known_card_ids or set()
        finder = getattr(store, "find_similar_card", None)
        merge_threshold = float(getattr(self.cfg, "card_merge_similarity",
                                        DEFAULT_CARD_MERGE_SIMILARITY))
        merge_enabled = callable(finder) and merge_threshold < 1.0
        for edit in edits[:max_cards]:
            try:
                cid = self._scoped_card_id(str(edit.get("card_id") or "").strip(), user_id)
                if not cid:
                    continue
                # If this edit would CREATE a new card, try to redirect it onto a clear existing twin
                # for the SAME user (embedding similarity) rather than minting a near-duplicate.
                if merge_enabled and cid not in known:
                    try:
                        text = _proposed_card_text(edit)
                        if text:
                            match = finder(text, user_id=user_id, min_score=merge_threshold)
                            if isinstance(match, str) and match.strip():
                                cid = match.strip()
                    except Exception:  # noqa: BLE001 — merge is best-effort: fall back to create
                        log.debug("card-merge similarity check failed", exc_info=True)
                fields: Dict[str, Any] = {}
                for _k in ("name", "description"):
                    v = edit.get(_k)
                    if isinstance(v, str) and v.strip():
                        fields[_k] = v.strip()
                add_items = [it for it in (edit.get("add") or []) if isinstance(it, dict)]
                # STAMP each new item with NOW. The card content model ranks and trims by ``ts``, and
                # an item that arrives without one is treated as maximally old: unstamped references
                # rank below anything dated and are the first trimmed, so a run's freshly learned
                # references would decay out of the card they were written to. The updater LLM has no
                # clock, so the brain stamps them.
                _now = time.time()
                for _it in add_items:
                    try:
                        if not float(_it.get("ts") or 0.0) > 0.0:
                            _it["ts"] = _now
                    except (TypeError, ValueError):
                        _it["ts"] = _now
                replace_in = [r for r in (edit.get("replace") or []) if isinstance(r, dict)]
                remove_ids = [str(r) for r in (edit.get("remove") or [])
                              if isinstance(r, (str, int))]
                # Bound total operations per card so one card can't run away.
                budget = max_edits
                add_items = add_items[:budget]
                budget -= len(add_items)
                replace_pairs: List[Tuple[str, Dict[str, Any]]] = []
                for r in replace_in[:max(0, budget)]:
                    item_id = str(r.get("item_id") or "").strip()
                    item = r.get("item")
                    if item_id and isinstance(item, dict):
                        replace_pairs.append((item_id, item))
                budget -= len(replace_pairs)
                remove_ids = remove_ids[:max(0, budget)]
                if not (fields or add_items or replace_pairs or remove_ids):
                    continue
                ok = store.update_card(
                    cid,
                    add=add_items or None,
                    replace=replace_pairs or None,
                    remove=remove_ids or None,
                    fields=fields or None,
                    scope_tags=scope_tags or None,
                )
                if ok:
                    written += 1
            except Exception:  # noqa: BLE001 — one bad edit must not stop the rest
                log.debug("applying a card edit failed", exc_info=True)
        if written:
            log.debug("post-deep card update wrote %d card(s)", written)
        return written

    @staticmethod
    def _render_cards_for_updater(cards: List[Dict[str, Any]]) -> str:
        """Render the user's current relevant cards (id + title + a few file pointers) for the
        updater prompt, so it can target existing card_ids and correct stale items. Never raises."""
        if not cards:
            return ""
        lines: List[str] = []
        for c in cards[:8]:
            if not isinstance(c, dict):
                continue
            cid = c.get("id", "?")
            title = c.get("title") or c.get("summary") or ""
            files = [str(f) for f in (c.get("files") or [])][:5]
            line = f"- [{cid}] {title}".rstrip()
            if files:
                line += "  (files: " + ", ".join(files) + ")"
            lines.append(line)
        return "\n".join(lines)

    def _update_cards_after_deep_async(
        self,
        *,
        request: str,
        executed: str,
        future_context: str,
        ctx_meta: Optional[Dict[str, Any]],
        emit: Optional[_Emitter] = None,
        observations: Optional[List[str]] = None,
        teaches_new_facts: bool = True,
    ) -> None:
        """Spawn the post-deep card updater in a BACKGROUND daemon thread so it never blocks the
        returned answer. Inert when the updater is not active. Best-effort: a failure to even start
        the thread is swallowed. A quiet STATUS tick is emitted; it never surfaces as a user message.
        """
        if not self._card_updater_active():
            return

        def _bg() -> None:
            try:
                n = self._update_cards_after_deep(
                    request=request, executed=executed,
                    future_context=future_context, ctx_meta=ctx_meta,
                    observations=observations, teaches_new_facts=teaches_new_facts)
                if n and emit is not None:
                    try:
                        emit.status(f"Updated {n} context card(s) for next time.")
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001 — the background updater must never raise out
                log.debug("background card updater failed", exc_info=True)

        try:
            threading.Thread(target=_bg, daemon=True).start()
        except Exception:  # noqa: BLE001
            pass

    def _update_context_cards_after_deep(
        self,
        deep_result: OrchestratorResult,
        context_meta: Optional[Dict[str, Any]],
        project_path: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """Update context cards with files modified during deep execution.

        Runs in background (non-blocking). Discovers edited files via multiple strategies:
        1. Claude Code: parses ~/.claude/projects/<project>/<session_id>/messages.jsonl
        2. Other runners: extracts from result metadata or parses prompted output format
        Then categorizes them into the context cards used for this task and updates
        card JSON files idempotently.

        Args:
            deep_result: The OrchestratorResult from deep execution
            context_meta: Metadata about context cards used for this task
            project_path: Optional path to project (for Claude Code session parsing)
            session_id: Optional session ID (for Claude Code session parsing)
        """
        if not context_meta or not context_meta.get("cards"):
            return

        def background_update():
            try:
                updater = self.edited_files_card_updater
                if updater is None:
                    return
                updater(
                    deep_result,
                    context_meta,
                    project_path=project_path,
                    session_id=session_id,
                    model_provider=self.provider,
                    registry=self.registry,
                )
            except Exception as e:
                log.debug(f"Background context card update failed (non-blocking): {e}")

        # Launch in background (fire and forget, doesn't block task result)
        thread = threading.Thread(target=background_update, daemon=True)
        thread.start()

    def _kickoff_card_update(self, res: OrchestratorResult, plan: Optional[PlanDecision],
                             user_message: str, ctx_meta: Optional[Dict[str, Any]],
                             emit: Optional[_Emitter],
                             gathered: Optional[List[Dict[str, Any]]] = None) -> None:
        """Build the updater's input bundle from a finished deep result and kick off the async card
        updater. The request is the user's goal/condition; ``executed`` is the brief + each deep
        result's output; the FUTURE-CONTEXT bullets come from each result's ``future_context`` field
        (filled at the runner seam by ``_normalize_future_context``, from EITHER channel). Inert when
        the updater is not active or nothing executed. Never raises.

        WHAT THE TURN MAY TEACH is decided here, from structured facts only (see
        ``core/card_learning.py``): the deep runs' verified verdict (``DeepResult.met``), their write
        receipts (``observations`` / ``changed_nothing``) and this turn's ``gathered`` reads. Those
        observations are also what the updater is given as the material for a new fact, so a card
        cannot end up holding a sentence the reply invented."""
        try:
            if not self._card_updater_active():
                return
            results = list(getattr(res, "deep_results", None) or [])
            if not results:
                return
            request = (getattr(plan, "goal", None) or "").strip() or user_message
            brief = (getattr(plan, "deep_brief", None) or "").strip()
            outputs = [(d.output or "").strip() for d in results if (d.output or "").strip()]
            executed_parts: List[str] = []
            if brief:
                executed_parts.append("BRIEF:\n" + brief)
            if outputs:
                executed_parts.append("RESULT:\n" + "\n\n".join(outputs))
            future = "\n".join(
                fc for fc in (_deep_future_context(d) for d in results) if fc
            )
            self._update_cards_after_deep_async(
                request=request,
                executed="\n\n".join(executed_parts),
                future_context=future,
                ctx_meta=ctx_meta,
                emit=emit,
                observations=turn_observations(results, gathered),
                teaches_new_facts=turn_teaches_new_facts(results, gathered),
            )
        except Exception:  # noqa: BLE001 — kicking off the updater must never break the turn
            log.debug("card-update kickoff failed", exc_info=True)

    def _kickoff_anticipation(self, *, user_message: str,
                              ctx_meta: Optional[Dict[str, Any]]) -> None:
        """Turn-end half of the ANTICIPATION engine (see core/anticipation.py): learn this turn's
        ask pattern, then plan + precompute the next predictions, in a BACKGROUND daemon thread
        (mirroring ``_update_cards_after_deep_async``) so it never blocks the returned answer.
        The thread is registered like the background context-index threads, so a consumer/test
        joins it via ``config.shutdown_background_index()`` (which asks the Anticipator to stop
        between bundles via ``close()``). SINGLE-FLIGHT per orchestrator: while the previous
        turn's learn/plan thread is still running, this turn's kickoff is skipped entirely (a
        dropped best-effort learning signal, never a queue), so fast turns cannot pile up threads
        racing the assembler and the scope files. Ranking sees only the USER's own words: the
        model's reply text is deliberately NOT fed in (its extra vocabulary dilutes the keyword
        similarity enough to suppress serving; the thing predicted is the user's next ask, so the
        user's asks are the topic signal). Inert when the flag is off or nothing is wired; a
        failure to even start the thread is swallowed. Never raises."""
        if self.anticipator is None or not self.cfg.anticipation_enabled:
            return
        try:
            prev = getattr(self, "_anticipation_thread", None)
            if prev is not None and prev.is_alive():
                log.debug("anticipation kickoff skipped: previous learn/plan still running")
                return
            keys = self._anticipation_scope_keys(ctx_meta)
            if not keys:
                return
            anticipator = self.anticipator
            recent_texts = [user_message] if user_message else []

            def _bg() -> None:
                try:
                    bg_now = datetime.now()
                    anticipator.learn(user_message, keys, now=bg_now)
                    anticipator.plan_next(keys, recent_texts, now=bg_now)
                    # OPTIONAL one-LLM-call refresh (no-op unless a refiner is wired): refine the
                    # just-planned predictions' display text, drop the conversation-obsoleted ones,
                    # and add a few follow-ups. Kept inside this single-flight background thread so
                    # it stays off the response path and never exceeds one model call per turn.
                    anticipator.refresh(keys, recent_texts, now=bg_now)
                except Exception:  # noqa: BLE001 -- background learning must never raise out
                    log.debug("background anticipation learn/plan failed", exc_info=True)

            thread = threading.Thread(target=_bg, daemon=True, name="qar-anticipation")
            self._anticipation_thread = thread
            thread.start()
            try:
                # Register with the owned-background-thread registry (injected by config, since core
                # must not import it). shutdown_background_index() will close() the anticipator and
                # join this thread, the same ownership contract index threads have.
                if self.background_thread_registrar is not None:
                    self.background_thread_registrar(thread, anticipator)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 -- kicking off anticipation must never break the turn
            log.debug("anticipation kickoff failed", exc_info=True)
