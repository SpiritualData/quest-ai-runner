"""The planner step and the reach judge that gates it.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import copy
import threading
from typing import Any, Dict, List, Optional, Sequence
from concurrent.futures import Future, TimeoutError as FuturesTimeoutError
from collections import OrderedDict

from .planner_cascade import (
    CONFIDENCE_TOOL_FIELD,
    PLANNER_CONFIDENCE_INSTRUCTION,
    REVIEW_TOOL,
    apply_review,
    build_review_digest,
    escalation_spec,
    should_escalate,
)
from .planner_prompt import (
    DEFERRED_DEEP_INLINE_SEMANTICS,
    DEFERRED_DEEP_QUEUED_SEMANTICS,
    MODEL_TIER_BLOCK,
    PLANNER_DECISION_RUBRIC,
    _BRAINSTORM_EXIT_SIGNAL_NOTE,
    _BRAINSTORM_PLANNER_NOTE,
    _HOLD_OFF_PLANNER_NOTE,
    _MODE_SIGNAL_PLANNER_BLOCK,
    _RATIONALE_INSTRUCTION_NARRATE,
    _RATIONALE_INSTRUCTION_NARRATE_REPLAN,
    _RATIONALE_INSTRUCTION_PLAIN,
)
from .planner_context import (
    PLANNER_TOOLS_HEAD,
    _render_gathered_for_planner,
    already_said_tail,
    planner_links_block,
    planner_web_block,
)
from .adapters import (
    PlanDecision,
    STEP_JUDGE,
    STEP_PLAN,
    ScopedThreadPoolExecutor as ThreadPoolExecutor,
    plan_with_step,
)
from .reach_judge import (
    REACH_JUDGE_TOOL,
    judge_prompt,
    normalize_verdict,
    parse_judge_text,
    verdict_block,
)
from .prompt_layers import compose_layers
from .orchestrator_constants import log
from .orchestrator_events import _REPLAN_CONTEXT_REF, _REPLAN_TRANSCRIPT_REF
from .orchestrator_capabilities import provider_call_accepts_layers, provider_call_accepts_tier
from .orchestrator_decisions import normalize_decision
from .orchestrator_decide_tool import decide_tool_for, planner_prompt_for_profile


class PlanningMixin:
    """The planner step and the reach judge that gates it."""

    def _plan(self, user_message: str, transcript: str, context_view: str,
              gathered: List[Dict[str, Any]], *, step: int = 0,
              narrate: bool = False, persona: str = "",
              already_said: Optional[List[str]] = None,
              brainstorm: bool = False,
              user_vetoed_task: bool = False,
              card_thread_block: str = "",
              tool_pins: Sequence[str] = ()) -> PlanDecision:
        # Step 1 (step == 0) always sees the FULL transcript + context_view. On later re-plan
        # steps, if the consumer opted in, swap the (unchanged) transcript + context_view for a
        # short reference note — the planner's job there is to react to the NEW gathered
        # observations, not to re-read context it already saw. The ANSWER path is untouched.
        plan_transcript = transcript
        plan_context = context_view
        if step > 0 and self.cfg.planner_abbreviate_repeat_context:
            if transcript:
                plan_transcript = _REPLAN_TRANSCRIPT_REF
            if context_view:
                plan_context = _REPLAN_CONTEXT_REF
        # When narration is on, the planner writes its `rationale` as the user-facing spoken beat
        # for this step (Approach B), in the selected rep's voice — so we layer the persona on top
        # and swap in the conversational rationale instruction. No extra LLM call: the beat rides on
        # the planning call the orchestrator already makes.
        # Mode signals are opt-in: with the flag off the planner prompt carries no MODE SIGNAL
        # block, the decide schema has no `mode_signal` field, and the brainstorm note (when a
        # consumer drives the mode from its own state) omits the exit-signal exception.
        mode_signal_block = _MODE_SIGNAL_PLANNER_BLOCK if self.cfg.mode_signals_enabled else ""
        # Same no-action contract, two different reasons, and the planner is told the true one:
        # "the user has asked to think out loud" is wrong (and steers toward a rambling reply) when
        # what actually happened is that they asked a crisp question and said not to open a task.
        if user_vetoed_task:
            brainstorm_note = _HOLD_OFF_PLANNER_NOTE
        else:
            brainstorm_note = _BRAINSTORM_PLANNER_NOTE
            if self.cfg.mode_signals_enabled:
                brainstorm_note += _BRAINSTORM_EXIT_SIGNAL_NOTE
        # The compact profile also sends the compact SCHEMA: the field descriptions repeat what
        # its own actions block already says, and on a provider that renders the schema into the
        # prompt they are paid for on every call.
        compact_plan = (self.cfg.planner_prompt_profile or "").strip().lower() == "compact"
        decide_tool = decide_tool_for(self.cfg.mode_signals_enabled,
                                      self.cfg.deferred_deep_queued,
                                      self.cfg.card_thread_enabled,
                                      tools=self.tools is not None,
                                      compact=compact_plan,
                                      web=self.web is not None)
        # THE CASCADE'S ONE EXTRA FIELD, present only when a consumer opted in, so a deployment
        # that does not use the cascade pays nothing for it. Copied before mutating: the schemas
        # above are module-level singletons on the no-variant path.
        if getattr(self.cfg, "planner_cascade", False):
            decide_tool = copy.deepcopy(decide_tool)
            decide_tool["input_schema"]["properties"]["confidence"] = dict(CONFIDENCE_TOOL_FIELD)
            # REQUIRED, not optional, and that was measured. Offered as an optional field with the
            # instruction in the prompt body, a live planner left it null on 381 of 384 decisions
            # (2026-10-05), so nothing could ever escalate. This is the same finding the card
            # thread field already recorded: an optional field is one a model quietly omits, and
            # every omission lands on the fail-safe. The fail-safe here (no confidence means no
            # escalation) is unchanged and still catches a malformed value; it just stops being
            # the norm. The description stays even under the compact schema, since this is the one
            # field whose whole purpose is a judgment rather than a value.
            required = decide_tool["input_schema"].setdefault("required", [])
            if "confidence" not in required:
                required.append("confidence")
        # The TOOLS block: only the tools relevant to THIS request (the registry narrows a large
        # catalog; the rest are searchable with a {"tools": ...} read). Layered in like the
        # brainstorm note so PLANNER_PROMPT keeps its format slots; absent entirely with no tools.
        tools_block = (PLANNER_TOOLS_HEAD + self.tools.render_planner_block(user_message, pins=tool_pins)
                       if self.tools is not None else "")
        # The WEB block: same AFTER-the-body placement as tools_block above, for the same reason
        # (a block placed before the action list is noticed but not chosen). Empty with no web
        # adapter wired, so the prompt is byte-for-byte unchanged for a deployment without one.
        web_block = "\n\n".join(
            b for b in (planner_web_block(self.web), planner_links_block(user_message, self.web)) if b)
        deferred_semantics = (DEFERRED_DEEP_QUEUED_SEMANTICS if self.cfg.deferred_deep_queued
                              else DEFERRED_DEEP_INLINE_SEMANTICS)
        # Per-idea threading: the TOPIC block (doctrine + this turn's candidate prior) is rendered
        # ONCE per turn by run() and passed in. Empty string when the consumer did not opt in, so
        # the prompt is byte-identical to a build without the feature.
        thread_block = card_thread_block if self.cfg.card_thread_enabled else ""
        planner_template = planner_prompt_for_profile(self.cfg.planner_prompt_profile)
        # MODEL TIER DISCIPLINE is only worth its tokens when some wired deep runner actually runs
        # against the ladder it describes (see ``model_tier_doctrine_applies``); otherwise it is
        # dead weight on every planner call (Quest's in-process chat runners never touch it).
        model_tier_block = MODEL_TIER_BLOCK if self.model_tier_doctrine_applies() else ""
        prompt = planner_template.format(
            user_message=user_message,
            transcript=plan_transcript or "(no prior messages)",
            context_view=plan_context or "(no context)",
            gathered=_render_gathered_for_planner(
                gathered, self.cfg.planner_recent_full, self.cfg.planner_compress_over,
                current_step=step),
            max_reads=self.cfg.max_reads_per_step,
            max_subq=self.cfg.max_subquestions,
            max_deep=self.cfg.max_deep_subtasks,
            mode_signal_block=mode_signal_block,
            card_thread_block=thread_block,
            deferred_deep_semantics=deferred_semantics,
            model_tier_block=model_tier_block,
            rationale_instruction=(
                (_RATIONALE_INSTRUCTION_NARRATE_REPLAN if step > 0 else _RATIONALE_INSTRUCTION_NARRATE)
                if narrate else _RATIONALE_INSTRUCTION_PLAIN),
        )
        # Bounded hard (see NARRATION_SAID_PLANNER_MAX): the only job of this block is stopping the
        # NEXT line from echoing the shape of the one or two just said, not an audit trail of the
        # whole turn's narration.
        said_tail = already_said_tail(already_said)
        preamble_parts: List[str] = []
        if brainstorm:
            preamble_parts.append(brainstorm_note)
        if narrate and persona.strip():
            preamble_parts.append(
                "--- SPEAK AS THIS PERSONA (for your `rationale` line only) ---\n"
                + persona.strip()[:1500]
            )
        if narrate and said_tail:
            preamble_parts.append(
                "--- MOST RECENTLY SAID OUT LOUD THIS TURN (do NOT repeat, echo, or paraphrase) ---\n"
                + "\n".join(f"• {s}" for s in said_tail)
            )
        if preamble_parts:
            prompt = "\n\n".join(preamble_parts) + "\n\n" + prompt
        # AFTER the planner body, not above it: the body's own action list (read/answer/deep/...)
        # is otherwise the last word before the decision, and a live run filled tool_calls with
        # the right call but still chose "answer", so nothing ran.
        if tools_block:
            prompt = prompt + "\n\n" + tools_block
        if web_block:
            prompt = prompt + "\n\n" + web_block
        if getattr(self.cfg, "planner_cascade", False):
            prompt = prompt + "\n\n" + PLANNER_CONFIDENCE_INSTRUCTION
        # THE REACH VERDICT, judged once per turn on a stronger tier and cached for the re-plan
        # steps, so a multi-step turn pays for it once. Appended AFTER the body: it is a fact about
        # THIS request, not doctrine, and it belongs beside the request rather than above the
        # action list.
        reach = self.reach_verdict(user_message)
        reach_text = verdict_block(reach, web_configured=self.web is not None)
        if reach_text:
            prompt = prompt + "\n\n" + reach_text
        model = ((getattr(self.cfg, "planner_model", "") or "").strip()
                 or self.registry.resolve_tier(self.cfg.planner_tier))
        provider = self.get_provider_for_model(model)
        # Cache-friendly layered shape (in addition to the flattened ``prompt`` fallback above): the
        # persona rides in the stable L1 head, the context view is the stable L2 layer, and the
        # planner instructions + message + gathered are the volatile L3 tail (with the context slot
        # relocated to a pointer, since the real context now sits above in L2). Only passed to a
        # provider that accepts ``layers``; the flattened ``prompt`` remains the faithful fallback so
        # a provider without the layered surface is byte-for-byte unchanged.
        plan_kwargs: Dict[str, Any] = {"model": model, "tool_schema": decide_tool}
        if provider_call_accepts_layers(provider.plan):
            plan_body = planner_template.format(
                user_message=user_message,
                transcript=plan_transcript or "(no prior messages)",
                context_view="(provided in the CONTEXT section above)",
                gathered=_render_gathered_for_planner(
                    gathered, self.cfg.planner_recent_full, self.cfg.planner_compress_over,
                    current_step=step),
                max_reads=self.cfg.max_reads_per_step,
                max_subq=self.cfg.max_subquestions,
                max_deep=self.cfg.max_deep_subtasks,
                mode_signal_block=mode_signal_block,
                card_thread_block=thread_block,
                deferred_deep_semantics=deferred_semantics,
                model_tier_block=model_tier_block,
                rationale_instruction=(
                    (_RATIONALE_INSTRUCTION_NARRATE_REPLAN if step > 0 else _RATIONALE_INSTRUCTION_NARRATE)
                    if narrate else _RATIONALE_INSTRUCTION_PLAIN),
            )
            tail_parts: List[str] = []
            if brainstorm:
                tail_parts.append(brainstorm_note)
            if narrate and said_tail:
                tail_parts.append(
                    "--- MOST RECENTLY SAID OUT LOUD THIS TURN (do NOT repeat, echo, or paraphrase) ---\n"
                    + "\n".join(f"• {s}" for s in said_tail)
                )
            tail_parts.append(plan_body)
            if tools_block:
                tail_parts.append(tools_block)
            if web_block:
                tail_parts.append(web_block)
            if reach_text:
                tail_parts.append(reach_text)
            plan_kwargs["layers"] = compose_layers(
                persona=(persona if (narrate and persona.strip()) else ""),
                context=plan_context or "",
                tail="\n\n".join(tail_parts),
            ).blocks()
        raw = plan_with_step(provider, prompt, step=STEP_PLAN, **plan_kwargs)
        if not raw and (getattr(self.cfg, "planner_model", "") or "").strip():
            # A pinned routing model that answered nothing (a mistyped id, an outage) gets ONE
            # retry on the planner tier, instead of every step silently taking the fail-safe.
            log.warning("Pinned planner model %r returned no decision; retrying on tier %r",
                        model, self.cfg.planner_tier)
            model = self.registry.resolve_tier(self.cfg.planner_tier)
            provider = self.get_provider_for_model(model)
            plan_kwargs["model"] = model
            if not provider_call_accepts_layers(provider.plan):
                plan_kwargs.pop("layers", None)
            raw = plan_with_step(provider, prompt, step=STEP_PLAN, **plan_kwargs)
        decision = normalize_decision(raw or {}, self.cfg, tools_enabled=self.tools is not None,
                                      web_enabled=self.web is not None)
        return self.cascade_review(decision, user_message, plan_context, gathered)

    # --- the reach judge (opt in; see core/reach_judge.py) --------------------

    #: How many distinct requests' verdicts one Orchestrator keeps. A long-lived orchestrator serves
    #: many turns, and an unbounded per-message cache is a slow leak.
    REACH_VERDICT_CACHE_SIZE = 256

    def reach_judge_active(self) -> bool:
        return bool(getattr(self.cfg, "planner_reach_judge", False)
                    and (getattr(self.cfg, "read_reach_summary", "") or "").strip())

    def prefetch_reach_verdict(self, user_message: str) -> Optional[Future]:
        """Start the reach judge in the background and return its future (None when it is off).

        The judge's only inputs are the literal request and the consumer's reach summary, both
        known at the top of a turn, so there is no reason for it to sit serially in front of the
        first plan: run() starts it here, it overlaps request understanding and context assembly,
        and ``reach_verdict`` collects it. Idempotent per message: a second call for the same
        request returns the SAME future, so a turn never pays for the judge twice. The pool is a
        usage-scoped one created on the calling thread, so the judge's tokens are billed to the
        turn that asked for them.
        """
        if not self.reach_judge_active():
            return None
        key = (user_message or "")[:500]
        lock = self.__dict__.setdefault("reach_verdict_lock", threading.Lock())
        with lock:
            cache = self.__dict__.setdefault("reach_verdict_cache", OrderedDict())
            future = cache.get(key)
            if future is not None:
                cache.move_to_end(key)
                return future
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                future = pool.submit(self.judge_reach, user_message)
            finally:
                # wait=False: the submitted judge still runs to completion; this only stops the
                # pool from accepting more work, so its thread exits when the judge returns.
                pool.shutdown(wait=False)
            cache[key] = future
            while len(cache) > self.REACH_VERDICT_CACHE_SIZE:
                cache.popitem(last=False)
            return future

    def peek_reach_verdict(self, user_message: str) -> Optional[Dict[str, Any]]:
        """A non-blocking read of an ALREADY-FINISHED reach verdict for ``user_message``, or None.

        Never starts the judge and never waits on it: only a verdict some earlier
        ``prefetch_reach_verdict``/``reach_verdict`` call already resolved is returned. Built for
        callers that run LATER in the same turn than the planner's own (blocking) reach-verdict
        read -- e.g. a deep goal's own context assembly, or a widening retry -- where the judge's
        single small call is essentially guaranteed to have finished already, so this is free. A
        caller at the very top of the turn (the reach judge and the turn-start context prefetch
        both kick off around the same moment) will usually see a miss here, which is correct: this
        must never add latency by waiting, so it only ever reuses a verdict that was free to read.
        """
        try:
            key = (user_message or "")[:500]
            cache = self.__dict__.get("reach_verdict_cache")
            if not cache:
                return None
            future = cache.get(key)
            if future is None or not future.done():
                return None
            return future.result()
        except Exception:  # noqa: BLE001
            return None

    def gate_docs_fallback(self, ctx_meta: Optional[Dict[str, Any]], user_message: str) -> None:
        """Best-effort: stamp ``ctx_meta["skip_corpus_fallback"] = True`` IN PLACE when a
        non-blocking peek at an ALREADY-RESOLVED reach verdict for ``user_message`` says this
        turn's request does not live inside this corpus at all ("outside" or "world").

        This is the one place that decides whether a context assembly reached through
        ``ctx_meta`` should skip ``FileContextStore``'s keyword-grep corpus fallback (see its
        ``skip_corpus_fallback`` meta key): that fallback has no relevance signal of its own
        beyond "the keyword appears somewhere in the corpus", so it should not run at all for a
        turn the reach judge already settled as not being about this corpus. Never blocks (see
        ``peek_reach_verdict``) and never overrides a value the caller already set. No-op when
        ``ctx_meta`` is None.
        """
        if ctx_meta is None or "skip_corpus_fallback" in ctx_meta:
            return
        verdict = self.peek_reach_verdict(user_message)
        if verdict and verdict.get("reach") != "inside":
            ctx_meta["skip_corpus_fallback"] = True

    def reach_verdict(self, user_message: str) -> Optional[Dict[str, Any]]:
        """The stronger tier's answer to where what this request needs lives, or None.

        Collects the judge run() already started (see ``prefetch_reach_verdict``), or starts it now
        for a caller that plans without run(). Cached on the message itself, so the re-plan steps
        of a multi-step turn reuse the one verdict rather than paying for it again. Every failure
        path returns None, including a judge still running after
        ``planner_reach_judge_timeout_seconds``, which leaves the planner prompt byte-identical to
        a run with no judge: this is an optimisation and must never be able to take a turn down.
        """
        future = self.prefetch_reach_verdict(user_message)
        if future is None:
            return None
        timeout = float(getattr(self.cfg, "planner_reach_judge_timeout_seconds", 20.0) or 20.0)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            log.warning("Reach judge still running after %.1fs, planning without a verdict",
                        timeout)
            # Settle it as "no verdict" for the rest of the turn, so a stuck judge costs this one
            # wait and not one per re-plan step.
            settled: Future = Future()
            settled.set_result(None)
            key = (user_message or "")[:500]
            with self.__dict__.setdefault("reach_verdict_lock", threading.Lock()):
                cache = self.__dict__.setdefault("reach_verdict_cache", OrderedDict())
                if cache.get(key) is future:
                    cache[key] = settled
        except Exception as e:  # noqa: BLE001
            log.warning("Reach judge failed, planning without a verdict: %s: %s",
                        type(e).__name__, str(e)[:200])
        return None

    def judge_reach(self, user_message: str) -> Optional[Dict[str, Any]]:
        """Ask the judging tier the one reach question. Never raises; None on any failure."""
        summary = (getattr(self.cfg, "read_reach_summary", "") or "").strip()
        verdict = None
        try:
            model = self.registry.resolve_tier(self.cfg.planner_reach_judge_tier)
            provider = self.get_provider_for_model(model)
            kwargs: Dict[str, Any] = {"model": model, "tool_schema": REACH_JUDGE_TOOL}
            if provider_call_accepts_tier(provider.plan):
                kwargs["tier"] = self.cfg.planner_reach_judge_tier
            raw = plan_with_step(provider, judge_prompt(user_message, summary,
                                                        web_configured=self.web is not None),
                                 step=STEP_JUDGE, **kwargs)
            verdict = normalize_verdict(raw) or parse_judge_text(raw)
        except Exception as e:  # noqa: BLE001
            log.warning("Reach judge failed, planning without a verdict: %s: %s",
                        type(e).__name__, str(e)[:200])
        if verdict:
            log.info("Reach verdict: %s (covered_by=%s)", verdict["reach"], verdict["covered_by"])
        return verdict

    # --- the overseer cascade (opt in; see core/planner_cascade.py) ------------

    def cascade_review(self, decision: PlanDecision, user_message: str, context_view: str,
                       gathered: List[Dict[str, Any]]) -> PlanDecision:
        """Re-decide a LOW-CONFIDENCE routing decision on a stronger model, from a short digest.

        Off unless ``cfg.planner_cascade``. Returns ``decision`` untouched when the cascade is off,
        when the planner reported no confidence or a confidence the operator did not ask to review,
        or when the review call fails: a failed overseer is never a failed turn.
        """
        self.planner_cascade_reviews = getattr(self, "planner_cascade_reviews", 0)
        self.last_plan_cascaded = False
        if not getattr(self.cfg, "planner_cascade", False):
            return decision
        if not should_escalate(decision, escalation_spec(self.cfg.planner_cascade_escalate_on)):
            return decision
        digest = build_review_digest(
            user_message, decision, PLANNER_DECISION_RUBRIC, context_view, gathered,
            max_chars=self.cfg.planner_cascade_digest_chars, web_configured=self.web is not None)
        model = self.registry.resolve_tier(self.cfg.planner_cascade_tier)
        review_provider = self.get_provider_for_model(model)
        review_kwargs: Dict[str, Any] = {"model": model, "tool_schema": REVIEW_TOOL}
        if provider_call_accepts_tier(review_provider.plan):
            review_kwargs["tier"] = self.cfg.planner_cascade_tier
        try:
            raw = plan_with_step(review_provider, digest, step=STEP_JUDGE, **review_kwargs)
        except Exception as e:  # noqa: BLE001
            log.warning("Planner cascade review failed, keeping the cheap decision: %s: %s",
                        type(e).__name__, str(e)[:200])
            return decision
        self.planner_cascade_reviews += 1
        self.last_plan_cascaded = True
        reviewed = apply_review(decision, raw if isinstance(raw, dict) else None)
        if reviewed is not decision:
            log.info("Planner cascade corrected %s -> %s (confidence %s)",
                     decision.action, reviewed.action, decision.confidence)
        return reviewed
