"""Assembling and shaping the context a deep run is handed.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Dict, List, Optional, Tuple

from .recent_context import (
    GLOBAL_SCOPE_KEY,
    build_item_usage_hint,
    conv_scope_key,
    filter_relevant,
    quest_scope_key,
    render_recent_cards,
)
from hubofhubs.ports.scope import ScopedThreadPoolExecutor as ThreadPoolExecutor
from .adapters_bases import runner_uses_deep_model
from .orchestrator_capabilities import (
    context_assembly_timeout_seconds,
    record_context_assembly_timeout,
    with_assembly_deadline,
)
from .orchestrator_decide_tool import DEFERRED_RUNNER_KEY
from .orchestrator_constants import RECIPE_SKIP_GOAL_CONTEXT, log
from .scope_tags import as_tag_list

# Said when a deep run's own context could not be had in time, so the person is not left thinking
# the worker was given it. The log WARNING carries the detail.
CONTEXT_DROPPED_STATUS = "Going on without that context. The search ran long."


class DeepContextMixin:
    """Assembling and shaping the context a deep run is handed."""

    @staticmethod
    def _drain_pending(pending_inputs: Optional[Callable[[], List[str]]]) -> str:
        """Pull any NEW user messages that arrived mid-run (the consumer supplies the callable) and
        render them as a block to fold into the next attempt, so a long-running goal loop picks up
        what the user said after it started. Returns "" when none / not wired. Never raises."""
        if pending_inputs is None:
            return ""
        try:
            msgs = [str(m).strip() for m in (pending_inputs() or []) if str(m).strip()]
        except Exception:  # noqa: BLE001 — draining must never break the run
            return ""
        if not msgs:
            return ""
        return ("--- NEW MESSAGES FROM THE USER SINCE YOU STARTED (incorporate these) ---\n"
                + "\n".join(f"- {m}" for m in msgs))

    @staticmethod
    def _augment_brief(base_brief: str, prev_output: str, verdict: Dict[str, Any]) -> str:
        """Build the next attempt's brief from the verdict: the original brief PLUS what the last
        attempt produced, why it fell short, and the specific next action to take."""
        reason = (verdict.get("reason") or "the done-standard was not yet satisfied").strip()
        nxt = (verdict.get("next_action")
               or "complete the remaining work so the goal is fully met.").strip()
        return (
            f"{base_brief}\n\n"
            "--- PREVIOUS ATTEMPT DID NOT YET MEET THE GOAL ---\n"
            f"What it reported:\n{(prev_output or '').strip()[:1500]}\n\n"
            f"Why it fell short: {reason}\n"
            f"Do this now: {nxt}"
        )

    @staticmethod
    def _continuation_brief(base_brief: str, verdict: Optional[Dict[str, Any]] = None) -> str:
        """The brief for CONTINUING a worker that ran out of turns, in its own resumed session.

        Deliberately short. The worker is resuming: it already holds the brief, everything it read,
        and every edit it made, so re-sending the full context (what ``_augment_brief`` does for a
        cold re-run) would only spend its fresh budget re-reading what it already knows. What it
        does not know is that it was cut off, so say that, and hand it the verifier's specific next
        action when there is one."""
        nxt = ((verdict or {}).get("next_action") or "").strip()
        parts = [
            "--- CONTINUE: YOU RAN OUT OF TURNS, YOU DID NOT FAIL ---",
            "Your previous run ended because it used its whole turn budget, not because anything "
            "went wrong. You are resuming that same session with a larger budget, so everything "
            "you already read, decided and changed is still yours. Do not start over and do not "
            "re-explore what you have already read: pick up exactly where you stopped.",
        ]
        if nxt:
            parts.append(f"What still has to happen: {nxt}")
        parts.append(
            "Finish the remaining work, verify it yourself, and end with a concrete summary of "
            "what you changed. If you cannot finish, say precisely what is left."
        )
        parts.append(f"For reference, the original task was:\n{base_brief.strip()[:1200]}")
        return "\n\n".join(parts)

    # --- warm recent-context scoping (shared by the main turn and per-goal deep context) -----

    @staticmethod
    def _meta_quest_ids(meta: Dict[str, Any]) -> List[str]:
        """The deduped quest ids in scope for ``meta``: a single ``quest_id`` plus any list of
        ``quest_ids`` (a conversation scoped to several quests at once). Never raises."""
        ids: List[str] = []
        try:
            single = meta.get("quest_id")
            if single:
                ids.append(single)
            for qid in (meta.get("quest_ids") or []):
                if qid and qid not in ids:
                    ids.append(qid)
        except Exception:  # noqa: BLE001
            return ids
        return ids

    def _recent_scope_keys(self, ctx_meta: Optional[Dict[str, Any]]) -> List[str]:
        """The WARM recent-context scope keys for this run/goal (see core/recent_context.py):
        ``conv:<conv_id>`` when a conversation id is in scope, ``quest:<quest_id>`` for every quest
        id in scope (``quest_id`` and/or the list ``quest_ids``), PLUS always ``"global"``
        (everything recently selected anywhere), unless the consumer turned cross-conversation
        memory off via ``cfg.recent_context_global_enabled``. A turn/goal with no quest id in scope
        still reads/records global (as long as global is enabled). Never raises; [] when
        recent-context is off entirely."""
        keys: List[str] = []
        try:
            meta = ctx_meta or {}
            conv_id = meta.get("conv_id")
            if conv_id:
                keys.append(conv_scope_key(conv_id))
            for quest_id in self._meta_quest_ids(meta):
                keys.append(quest_scope_key(quest_id))
            if self.cfg.recent_context_global_enabled:
                keys.append(GLOBAL_SCOPE_KEY)
        except Exception:  # noqa: BLE001
            return []
        return keys

    def _anticipation_scope_keys(self, ctx_meta: Optional[Dict[str, Any]]) -> List[str]:
        """The ANTICIPATION engine's scope keys for this run (see core/anticipation.py):
        ``conv:<conv_id>`` when a conversation id is in scope, ``quest:<quest_id>`` for every quest
        id in scope (``quest_id`` and/or the list ``quest_ids``), plus always ``"global"`` --
        narrowest first, which is the precedence ``Anticipator.observe`` honors on a score tie. Same
        key vocabulary as the warm recent-context store (``core.recent_context``). Never raises; []
        on any failure."""
        keys: List[str] = []
        try:
            meta = ctx_meta or {}
            conv_id = meta.get("conv_id")
            if conv_id:
                keys.append(conv_scope_key(conv_id))
            for quest_id in self._meta_quest_ids(meta):
                keys.append(quest_scope_key(quest_id))
            keys.append(GLOBAL_SCOPE_KEY)
        except Exception:  # noqa: BLE001
            return []
        return keys

    # --- per-goal context selection (each deep goal gets its OWN context) -----

    def _assemble_for_goal(self, goal: str, *,
                           ctx_meta: Optional[Dict[str, Any]]) -> str:
        """Select PER-GOAL context for a single deep goal condition (text only). Thin wrapper over
        ``_assemble_for_goal_with_cards`` for any caller that only needs the rendered block."""
        text, _cards = self._assemble_for_goal_with_cards(goal, ctx_meta=ctx_meta)
        return text

    def bounded_goal_assembly(
        self, goal_text: str, goal_meta: Dict[str, Any], *,
        turn_context: Optional[Any] = None, ctx_meta: Optional[Dict[str, Any]] = None,
        status: Optional[Callable[[str], None]] = None, gathering_text: str = "",
    ) -> Optional[Any]:
        """The context assembler's result for ONE deep goal, or None. Never raises, and never
        waits past ``context_assembly_timeout_seconds()`` -- the same wall-clock budget the
        turn-start assembly gets, with the same soft ``assembly_deadline`` in ``meta`` so a
        deadline-aware assembler hands back a PARTIAL result instead of overrunning it.

        This used to be a bare ``assemble()`` call with no bound at all, run BEFORE the worker
        spawned and after the turn-start assembly had already timed out on the same store: on a
        card store large enough to blow the budget once, the deep run sat silent for minutes
        competing with its own still-running first attempt.

        ``turn_context`` (the run's ``TurnCardCache``) is passed only for a deep run with exactly
        ONE goal, whose request is the one the turn-start assembly already served: that assembly
        is reused if it finished, waited for (bounded) if it is still running, and never run a
        second time. It is ignored when its scope tags differ from ``ctx_meta``'s, so a result
        assembled for one quest can never ground a run scoped to another. A fan-out subgoal has
        its own focus and always assembles for itself.

        ``status`` is told (once, with ``gathering_text``) just before this actually waits on an
        assembly, and again if it gives up, so a slow store is never a silent pause."""
        budget = context_assembly_timeout_seconds()
        said_gathering = False

        def say(text: str) -> None:
            if status is None or not text:
                return
            try:
                status(text)
            except Exception:  # noqa: BLE001 -- a status callback must never break the run
                pass

        def announce_gathering() -> None:
            nonlocal said_gathering
            if not said_gathering:
                said_gathering = True
                say(gathering_text)

        def give_up(reason: str) -> None:
            timeout_count = record_context_assembly_timeout()
            log.warning(
                "Deep-run context assembly timed out after %.1fs %s; the deep run proceeds "
                "without that context (QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS to adjust; total "
                "timeouts this process: %d)", budget, reason, timeout_count)
            say(CONTEXT_DROPPED_STATUS)

        if (turn_context is not None
                and as_tag_list((getattr(turn_context, "meta", None) or {}).get("scope_tags"))
                == as_tag_list((ctx_meta or {}).get("scope_tags"))):
            assembled, origin = turn_context.turn_start_context(budget, on_wait=announce_gathering)
            if origin in ("reused", "joined"):
                log.info("Deep-run context: %s the turn-start assembly instead of assembling "
                         "this goal a second time", origin)
                return assembled
            if origin == "pending":
                give_up("waiting for the turn-start assembly, which is still running")
                return None
        announce_gathering()
        assembler = self.context_assembler
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(
                lambda: assembler.assemble(goal_text, meta=with_assembly_deadline(goal_meta)))
            return future.result(timeout=budget)
        except FuturesTimeoutError:
            give_up("with no partial result")
            return None
        except Exception:  # noqa: BLE001 (assembly must never break the run)
            log.debug("per-goal context assembly failed", exc_info=True)
            return None
        finally:
            try:
                pool.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass

    def _assemble_for_goal_with_cards(
        self, goal: str, *, ctx_meta: Optional[Dict[str, Any]],
        turn_context: Optional[Any] = None,
        status: Optional[Callable[[str], None]] = None, gathering_text: str = "",
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Select PER-GOAL context for a single deep goal condition. Renders a block from the
        wired ``context_assembler`` (targeting THIS goal, not the shared run-level message), the
        WARM recent-context store's own scoped warm set (see core/recent_context.py -- the SAME
        completeness guarantee a chat turn gets, so a deep/background task benefits from warm
        context too, not just the interactive turn), plus a relevant CURRENT-conversation slice
        from the wired ``conversation_store``. Returns ``(rendered_text, card_metadata)`` --
        ``card_metadata`` is the merged fresh + surviving-recent card list this goal's context
        actually included (fresh cards first, dedupe-fresh-wins), so the caller can ``record()``
        it back to the warm store after the goal completes (see ``run_one`` in ``_run_deep``), the
        same way the main turn does. Returns ``("", [])`` when nothing is wired or found. Never
        raises: a degraded source is simply skipped.

        The assembler read is BOUNDED (see ``bounded_goal_assembly``, which also explains
        ``turn_context``, ``status`` and ``gathering_text``); the warm-set merge and the
        conversation slice are local reads and unchanged."""
        if (ctx_meta or {}).get(RECIPE_SKIP_GOAL_CONTEXT):
            return "", []           # a saved operation: the runner grounds itself, no context search
        parts: List[str] = []
        fresh_card_ids: set = set()
        fresh_card_meta: List[Dict[str, Any]] = []
        goal_text = (goal or "").strip()

        # Load the scoped warm set ONCE (used both for the recent_item_usage hint below and for
        # the recent-turn merge further down), gated against the GOAL text (not the run-level
        # message -- each subgoal gets memory relevant to ITS own focus).
        recent_records: List[Dict[str, Any]] = []
        recent_hint: Dict[str, List[str]] = {}
        if self.recent_context is not None and self.cfg.recent_context_enabled and goal_text:
            try:
                scope_keys = self._recent_scope_keys(ctx_meta)
                if scope_keys:
                    recent_records = self.recent_context.load(scope_keys)
                    recent_hint = build_item_usage_hint(recent_records, goal_text)
            except Exception:  # noqa: BLE001
                log.debug("per-goal recent-context load failed", exc_info=True)
                recent_records, recent_hint = [], {}

        if self.context_assembler is not None and goal_text:
            try:
                # A fresh dict for THIS goal's hint: never inherit the run-level ``ctx_meta``'s own
                # ``recent_item_usage`` (built for the user's whole message, not this subgoal) when
                # this goal's own scoped load produced no hint.
                goal_meta = dict(ctx_meta or {})
                if recent_hint:
                    goal_meta["recent_item_usage"] = recent_hint
                else:
                    goal_meta.pop("recent_item_usage", None)
                assembled = self.bounded_goal_assembly(
                    goal_text, goal_meta, turn_context=turn_context, ctx_meta=ctx_meta,
                    status=status, gathering_text=gathering_text)
                if assembled is not None:
                    cv = self._materialize_deep_context(assembled)
                    if cv:
                        parts.append("--- CONTEXT SELECTED FOR THIS GOAL ---\n" + cv)
                    fresh_card_meta = [
                        cm for cm in (getattr(assembled, "card_metadata", None) or [])
                        if isinstance(cm, dict) and cm.get("id")
                    ]
                    fresh_card_ids = {cm.get("id") for cm in fresh_card_meta}
            except Exception:  # noqa: BLE001 — assembly must never break the run
                log.debug("per-goal context assembly failed", exc_info=True)

        recent_entries: List[Dict[str, Any]] = []
        if recent_records:
            try:
                # The turn's time_range (when ``_derive_goal_condition`` parsed one; see run())
                # rides in ctx_meta, so a deep goal's recent-context set honors the same hard
                # time filter as the main turn. Absent key: byte-for-byte today's behavior.
                goal_time_range = (ctx_meta or {}).get("time_range")
                filtered = filter_relevant(
                    recent_records, goal_text, is_followup=False,
                    max_cards=self.cfg.recent_context_max_cards,
                    time_range=goal_time_range)
                survivors = [r for r in filtered if r.get("id") not in fresh_card_ids]
                recent_text, recent_entries = render_recent_cards(
                    survivors, goal_text, time_range=goal_time_range)
                if recent_text:
                    parts.append(recent_text)
            except Exception:  # noqa: BLE001 — recent-context merge must never break the run
                log.debug("per-goal recent-context merge failed", exc_info=True)
                recent_entries = []

        conv_id = (ctx_meta or {}).get("conv_id")
        if self.conversation_store is not None and conv_id and goal_text:
            try:
                slc = self.conversation_store.current_slice(conv_id, goal)
                txt = (getattr(slc, "text", "") or "").strip()
                if txt:
                    parts.append("--- RELEVANT CONVERSATION FOR THIS GOAL ---\n" + txt)
            except Exception:  # noqa: BLE001 — store must never break the run
                log.debug("per-goal conversation slice failed", exc_info=True)
        return "\n\n".join(parts), fresh_card_meta + recent_entries

    @staticmethod
    def _materialize_deep_context(assembled: Any) -> str:
        """Render the per-goal DEEP context from the assembled ``card_metadata``, choosing PASTE vs
        POINTER per item. Never raises.

        Each card carries its VERBATIM ``rendered_section`` (its whole rendered block: summary + file
        listings + content + conventions) plus its structured items (each ``{type, why, locator,
        text, deliver, pointer_eligible}``). When a card has a ``rendered_section`` we paste it
        VERBATIM, except that each item the consolidator tagged ``deliver=="pointer"`` (only ever a
        file, since the worker can re-read it) has its pasted text fragment SWAPPED for a short
        pointer line, so the file's full body is not duplicated. A card WITHOUT a ``rendered_section``
        (e.g. a stub assembler or a deployment that emits only items) falls back to the prior
        item-only rebuild under a ``### <title>`` header. When the assembled context carries neither
        sections nor items (e.g. a file-only deployment), this falls back to the plain
        ``context_view`` (today's behavior, byte-for-byte), so the change is never worse.
        """
        def _label(it: dict) -> str:
            # WHAT the reference points at (a file's path, a collection's name/id). MUST mirror
            # adapters.card_content_render.locator_label; kept inline so the core brain never imports
            # an adapter. Keep the two in sync.
            itype = it.get("type", "note")
            loc = it.get("locator") if isinstance(it.get("locator"), dict) else {}
            if itype == "file":
                return str(loc.get("path") or "").strip()
            if itype == "collection":
                name = str(loc.get("name") or loc.get("collection") or "").strip()
                cid = str(loc.get("id") or "").strip()
                if name and cid:
                    return f"{name} ({cid})"
                return name or cid
            if itype == "conversation":
                return str(loc.get("conv_id") or loc.get("id") or "").strip()
            if itype == "query":
                return str(loc.get("query") or loc.get("text") or "").strip()
            if itype == "note":
                return ""
            return str(loc.get("name") or loc.get("id") or loc.get("path") or "").strip()

        def _frag(it: dict) -> str:
            # The exact fragment a content item contributed to a rendered section (header + indented
            # body). MUST mirror adapters.card_content_render.render_block_lines; kept inline so the
            # core brain never imports an adapter. Keep the two in sync.
            itype = it.get("type", "note")
            why = it.get("why", "")
            text = it.get("text", "") or ""
            label = _label(it)
            head = f"  - ({itype})"
            if label:
                head += f" {label}"
                if why:
                    head += f" -- {why}"
            elif why:
                head += f" {why}"
            lines = [head]
            for rl in text.splitlines() or [text]:
                lines.append(f"      {rl}")
            return "\n".join(lines)

        try:
            cms = [cm for cm in (getattr(assembled, "card_metadata", None) or [])
                   if isinstance(cm, dict)]
            has_sections = any(cm.get("rendered_section") for cm in cms)
            has_items = any(cm.get("items") for cm in cms)
            if not has_sections and not has_items:
                return (getattr(assembled, "context_view", "") or "").strip()
            blocks: List[str] = []
            for cm in cms:
                items = cm.get("items") or []
                rendered = cm.get("rendered_section") or ""
                if rendered:
                    # Paste the VERBATIM section; swap each pointer-delivered file item's fragment for
                    # a pointer line (guard: only swap if the fragment is found in the section).
                    section = rendered
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        locator = it.get("locator") if isinstance(it.get("locator"), dict) else {}
                        path = locator.get("path")
                        if it.get("deliver") == "pointer" and it.get("pointer_eligible") and path:
                            why = (it.get("why") or "").strip()
                            pointer_line = (
                                f"  - (file) {why} -> read this file fresh if needed: {path}"
                            )
                            frag = _frag(it)
                            if frag and frag in section:
                                section = section.replace(frag, pointer_line, 1)
                            else:
                                raw = it.get("text", "")
                                if raw and raw in section:
                                    section = section.replace(raw, pointer_line, 1)
                    if section.strip():
                        blocks.append(section)
                    continue
                # No rendered_section: prior item-only rebuild.
                if not items:
                    continue
                title = cm.get("title") or cm.get("id") or "context"
                lines: List[str] = []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    locator = it.get("locator") if isinstance(it.get("locator"), dict) else {}
                    path = locator.get("path")
                    if it.get("deliver") == "pointer" and it.get("pointer_eligible") and path:
                        why = (it.get("why") or "").strip()
                        # NB: a pointer is ALWAYS a file (the worker can open it itself).
                        lines.append(
                            f"- (file) {why} -> read this file fresh if needed: {path}"
                        )
                    else:
                        text = it.get("text") or ""
                        if text:
                            lines.append(text)
                if lines:
                    blocks.append(f"### {title}\n" + "\n".join(lines))
            rendered_all = "\n\n---\n\n".join(blocks).strip()
            return rendered_all or (getattr(assembled, "context_view", "") or "").strip()
        except Exception:  # noqa: BLE001 — materialization must never break the run
            return (getattr(assembled, "context_view", "") or "").strip()

    def _widen_for_goal(self, goal: str, query: str, round_idx: int, *,
                        ctx_meta: Optional[Dict[str, Any]]) -> str:
        """Pull MORE context for a retry that reported it lacked context. Widens by round:
        a fresh assembler read targeting the verifier's ``context_query``, WIDER conversation
        retrieval (the current slice plus related OTHER conversations in scope), and a targeted
        retrieval grep/read of the missing term. ``round_idx`` (1-based) grows the budget so each
        retry can see more than the last. Returns "" when nothing extra is found. Never raises."""
        q = (query or goal or "").strip()
        if not q:
            return ""
        parts: List[str] = []
        # 1) Re-run the assembler against the SPECIFIC missing context the verifier named.
        if self.context_assembler is not None:
            try:
                assembled = self.context_assembler.assemble(q, meta=ctx_meta or None)
                cv = (getattr(assembled, "context_view", "") or "").strip()
                if cv:
                    parts.append("--- ADDITIONAL CONTEXT (" + q[:80] + ") ---\n" + cv)
            except Exception:  # noqa: BLE001
                log.debug("widen assembler read failed", exc_info=True)
        # 2) Widen the conversation: the current slice PLUS related other conversations, allowing
        #    more characters/conversations on each successive round.
        conv_id = (ctx_meta or {}).get("conv_id")
        conv_scope = (ctx_meta or {}).get("conv_scope")
        if self.conversation_store is not None and conv_scope:
            try:
                rel = self.conversation_store.related_slices(
                    q, conv_scope, exclude_conv_id=conv_id or None,
                    max_convs=min(3 + round_idx, 8),
                    max_chars=6000 + 2000 * round_idx)
                txt = (getattr(rel, "text", "") or "").strip()
                if txt:
                    parts.append("--- RELATED CONVERSATIONS ---\n" + txt)
            except Exception:  # noqa: BLE001
                log.debug("widen related slices failed", exc_info=True)
        # 3) Targeted retrieval read: grep the named term across the corpus for the next attempt.
        if self.retrieval is not None:
            try:
                obs = self.retrieval.grep(q, max_hits=min(10 + 5 * round_idx, 40))
                hits = getattr(obs, "hits", None) or []
                if hits:
                    lines = []
                    for h in hits[:min(10 + 5 * round_idx, 40)]:
                        if isinstance(h, dict):
                            lines.append(f"{h.get('rel_path','?')}:{h.get('line_no','?')}: "
                                         f"{str(h.get('line','')).strip()[:200]}")
                    if lines:
                        parts.append("--- RETRIEVAL HITS (" + q[:80] + ") ---\n"
                                     + "\n".join(lines))
            except Exception:  # noqa: BLE001
                log.debug("widen retrieval grep failed", exc_info=True)
        return "\n\n".join(parts)

    # --- deep fan-out --------------------------------------------------------

    def _has_deep_execution_capability(self) -> bool:
        """Whether ANY execution path is wired: a single default ``deep_runner``, OR the named
        registry (``deep_runners`` + a ``deep_runner_classifier`` to pick among them).

        Several call sites used to test ``self.deep_runner is not None`` as "can we actually
        execute deep work". That was correct back when a single runner was the only wiring style,
        but it went STALE once the named-runner registry was added: a consumer that wires
        ``deep_runner=None`` plus ``deep_runners``/``deep_runner_classifier`` (the multi-runner
        dispatch style, e.g. one runner for in-app data ops, one for open-ended tasks) has real
        execution capability, but every ``self.deep_runner is not None`` gate treated it as "no
        runner configured" and silently skipped execution/remediation. This is the one place that
        answers "can we run deep work at all", used by every such gate.
        """
        return self.deep_runner is not None or bool(self.deep_runner_ladder) or bool(
            self.deep_runners and self.deep_runner_classifier is not None)

    def _has_deferred_queue_capability(self) -> bool:
        """Whether DEFERRED work can be handed off, even with no inline execution capability.

        A queued deployment (``deferred_deep_queued``) that registered its queue runner under
        ``deep_runners[DEFERRED_RUNNER_KEY]`` can hand deferred work off with nothing else wired:
        the hand-off PINS that runner via ``_run_deep(runner_override=...)``, so it needs neither a
        default ``deep_runner`` nor a ``deep_runner_classifier``. Without this, such a consumer hit
        the ``_has_deep_execution_capability`` gate on the deferred nets and the queue was simply
        unreachable. Deliberately NOT merged into ``_has_deep_execution_capability``: the other
        gates it guards (planner "deep", overseer escalate_deep, claim remediation) run work INLINE
        through the normal wiring, which a queue-only consumer genuinely does not have.
        """
        return bool(self.cfg.deferred_deep_queued and self.deep_runners
                    and self.deep_runners.get(DEFERRED_RUNNER_KEY) is not None)

    def model_tier_doctrine_applies(self) -> bool:
        """Whether ``MODEL_TIER_GATE`` is worth a planner call's tokens THIS deployment.

        The gate is about picking a model tier for a DEEP run. When every deep runner this
        orchestrator could hand work to declares ``uses_deep_model = False`` (Quest's in-process
        chat runners: they execute on Quest's own tiers, never on ``QAR_MODEL_*``), the whole block
        is dead weight on every single planner call -- there is no ladder for the planner's choice
        to affect. Checks the single default ``deep_runner``, the escalation ``deep_runner_ladder``,
        and every named runner in the ``deep_runners`` registry (whichever one a later classifier
        picks, the doctrine must already be right or already be absent).

        Fails OPEN (returns True, keeps the doctrine) when NO runner is known at all -- a bare
        orchestrator with nothing wired yet, or a consumer this check has not been taught about --
        rather than silently dropping guidance for an unfamiliar wiring. Only turns it off when we
        positively know every wired runner ignores the ladder.
        """
        runners: List[Any] = []
        if self.deep_runner is not None:
            runners.append(self.deep_runner)
        runners.extend(r for r in (self.deep_runner_ladder or []) if r is not None)
        runners.extend(r for r in (self.deep_runners or {}).values() if r is not None)
        if not runners:
            return True
        return any(runner_uses_deep_model(r) for r in runners)
