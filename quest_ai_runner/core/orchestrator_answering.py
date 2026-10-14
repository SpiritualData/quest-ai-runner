"""Composing answers: grounded answer, sub-question fan-out, post-work synthesis.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .guard import ExecutionRecord
from .adapters import (
    PlanDecision,
    STEP_REPLY,
    ScopedThreadPoolExecutor as ThreadPoolExecutor,
    answer_with_reasoning,
)
from .auxiliary_prompts import (
    REPLY_VOICE_SYSTEM,
    SYNTHESIZE_AFTER_DEEP_PROMPT,
    SYNTHESIZE_AFTER_QUEUED_PROMPT,
)
from .prompt_layers import compose_layers
from .orchestrator_events import _grounding_block, grounding_answer_tail, grounding_context_layer
from .orchestrator_capabilities import provider_call_accepts_layers
from .orchestrator_constants import log


class AnsweringMixin:
    """Composing answers: grounded answer, sub-question fan-out, post-work synthesis."""

    # --- answer generation (grounded; optional parallel sub-questions) -------

    def _answer_model(self, plan: PlanDecision, default_tier: str,
                      hint: Optional[str] = None) -> str:
        """Resolve the model id for an answer or deep step.

        Precedence (highest to lowest):
          1. ``hint`` — a per-run model string passed by the caller (e.g. from a task's stored
             ``model`` field). Opaque: the consumer's ModelProvider and ModelRegistry interpret it.
             Unknown tiers degrade gracefully to the registry's default (never raises).
          2. ``plan.model_tier`` — the planner's own choice for this step.
          3. ``default_tier`` — the caller's compile-time default: a tier name, ``"balanced"``
             at every call site.
        """
        tier = hint or plan.model_tier or default_tier
        return self.registry.resolve_tier(tier)

    def _grounded_answer(self, user_message: str, transcript: str, context_view: str,
                         gathered: List[Dict[str, Any]], model: str, partial: bool,
                         native_blocks: Optional[List[Dict[str, Any]]] = None,
                         rep_preamble: Optional[str] = None,
                         reply_directive: Optional[str] = None) -> str:
        messages = []
        if rep_preamble:
            messages.append({"role": "user", "content": rep_preamble})
        if transcript:
            messages.append({"role": "user", "content": transcript})
        messages.append({"role": "user", "content": _grounding_block(context_view, gathered, partial)})
        # The final user message carries the question; when native image blocks are present (an
        # image attachment going to a vision-capable model/provider) they ride along in the SAME
        # message as a content-block list, so the model sees the image alongside the question.
        if native_blocks:
            content: List[Dict[str, Any]] = list(native_blocks)
            content.append({"type": "text", "text": user_message})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user_message})
        provider = self.get_provider_for_model(model)
        # REPLY_VOICE_SYSTEM rides as the system prompt, not as another user turn: it has to sit
        # ABOVE the grounding/transcript blocks (which are phrased as instructions ABOUT the person)
        # so the model answers in its own voice instead of mirroring theirs.
        # Cache-friendly layered shape (in addition to the ``messages`` fallback): the persona is the
        # stable L1 head, the grounding CONTEXT is the stable L2 layer, and transcript + this turn's
        # gathered content + the new message are the volatile L3 tail. Skipped for a multimodal turn
        # (native image blocks can't be flattened into the tail) and for any provider without the
        # layered surface, both of which keep the untouched ``messages`` path.
        # ``reply_directive`` is a HOW-TO-REPLY instruction for this turn (today: the brainstorm
        # no-action acknowledgment). It rides on the SYSTEM prompt, with the rest of the reply
        # contract, NOT inside the grounding block: that block is introduced to the model as
        # material to answer FROM and to never mention, which is exactly the wrong frame for an
        # instruction the reply must obey and speak to.
        answer_kwargs: Dict[str, Any] = {
            "model": model,
            "system": (f"{REPLY_VOICE_SYSTEM}\n\n{reply_directive}" if reply_directive
                       else REPLY_VOICE_SYSTEM),
        }
        if not native_blocks and provider_call_accepts_layers(provider.answer):
            tail_parts: List[str] = []
            if transcript:
                tail_parts.append(transcript)
            tail_parts.append(grounding_answer_tail(gathered, partial))
            tail_parts.append(user_message)
            answer_kwargs["layers"] = compose_layers(
                persona=rep_preamble or "",
                context=grounding_context_layer(context_view),
                tail="\n\n".join(tail_parts),
            ).blocks()
        return answer_with_reasoning(provider, messages, step=STEP_REPLY, **answer_kwargs)

    def _synthesize_after_deep(self, user_message: str, *, prior_answer: str, deep_output: str,
                               transcript: str, model: str,
                               rep_preamble: Optional[str] = None) -> str:
        """Rewrite the final reply to REPORT what a (deferred) deep run actually did/produced.

        After a deferred deep run, the pre-deep answer is a proposal that usually reads as "shall I
        proceed?", while the real deliverable lives in the deep run's output. This folds that output
        back into one user-facing reply grounded in what was actually done, in the rep's voice. One
        LLM call. Never raises: on any failure it falls back to the deep output itself (the real
        work), and only then to the prior answer, so the user always sees the substance.
        """
        out = (deep_output or "").strip()
        try:
            prompt = SYNTHESIZE_AFTER_DEEP_PROMPT.format(
                request=user_message,
                proposal=(prior_answer or "(none)").strip()[:4000],
                result=out[:24000],
            )
            messages: List[Dict[str, Any]] = []
            if rep_preamble:
                messages.append({"role": "user", "content": rep_preamble})
            if transcript:
                messages.append({"role": "user", "content": transcript})
            messages.append({"role": "user", "content": prompt})
            provider = self.get_provider_for_model(model)
            synthesized = answer_with_reasoning(provider, messages, model=model,
                                                step=STEP_REPLY, system=REPLY_VOICE_SYSTEM)
            if isinstance(synthesized, str) and synthesized.strip():
                return synthesized.strip()
        except Exception:  # noqa: BLE001 — synthesis must never break the turn
            log.warning("post-deep synthesis failed; falling back to deep output", exc_info=True)
        return out or (prior_answer or "")

    def _synthesize_after_queued(self, user_message: str, *, prior_answer: str,
                                 handoff_output: str, transcript: str, model: str,
                                 rep_preamble: Optional[str] = None) -> str:
        """Rewrite the reply after a CONFIRMED deferred hand-off (queued deployments).

        The counterpart of ``_synthesize_after_deep`` for a deep runner that QUEUED the work as a
        background task instead of executing it: the reply must report the hand-off (queued, will
        report back when finished), never present the work as done. Only called after the enqueue
        is confirmed, so the fallback line may honestly say the work is queued. One LLM call;
        never raises.
        """
        try:
            prompt = SYNTHESIZE_AFTER_QUEUED_PROMPT.format(
                request=user_message,
                proposal=(prior_answer or "(none)").strip()[:4000],
                result=(handoff_output or "(hand-off confirmed)").strip()[:4000],
            )
            messages: List[Dict[str, Any]] = []
            if rep_preamble:
                messages.append({"role": "user", "content": rep_preamble})
            if transcript:
                messages.append({"role": "user", "content": transcript})
            messages.append({"role": "user", "content": prompt})
            provider = self.get_provider_for_model(model)
            synthesized = answer_with_reasoning(provider, messages, model=model,
                                                step=STEP_REPLY, system=REPLY_VOICE_SYSTEM)
            if isinstance(synthesized, str) and synthesized.strip():
                return synthesized.strip()
        except Exception:  # noqa: BLE001 — synthesis must never break the turn
            log.warning("post-queue synthesis failed; falling back to a plain hand-off note",
                        exc_info=True)
        note = ("I have queued this work to run in the background and will report back in this "
                "conversation when it finishes.")
        return ((prior_answer or "").strip() + "\n\n" + note).strip()

    def synthesize_task_report(self, request: str, deep_output: str, *,
                               transcript: str = "",
                               rep_preamble: Optional[str] = None,
                               tier: str = "balanced") -> str:
        """PUBLIC: write the finished-work report for a completed background task.

        Used by the runner's executor to fold a deep run's raw output into a message that reads
        as the AI reporting its own finished work (same prompt shape as the interactive
        after-deep synthesis), before posting the task's done message into the originating chat.
        ``tier`` resolves through the registry ("balanced" by default; the registry's tiers are
        "fast", "balanced" and "best"). Never raises: any failure returns the raw ``deep_output``
        unchanged.
        """
        try:
            model = self.registry.resolve_tier(tier)
            return self._synthesize_after_deep(
                request, prior_answer="", deep_output=deep_output,
                transcript=transcript, model=model, rep_preamble=rep_preamble)
        except Exception:  # noqa: BLE001 — a report rewrite must never break task reporting
            log.warning("task report synthesis failed; using raw output", exc_info=True)
            return deep_output

    def report_claims_unbacked(self, request: str, report_text: str,
                               exec_record: Optional["ExecutionRecord"]
                               ) -> Optional[bool]:
        """PUBLIC: claim-check a synthesized report against the run's execution record.

        Returns True when the report claims completed work the execution record does NOT back
        (``claims_unexecuted``), False when the claims are clean, and None when the check could
        not run (no record, LLM outage, parse failure). Callers must treat None as "do not trust
        the rewrite" and fall back to the verified raw output. Never raises.
        """
        if exec_record is None:
            return None
        try:
            verdict, _err = self._verify_goal(
                f"Honestly report the outcome of: {request}", request, report_text,
                exec_record=exec_record)
            if verdict is None:
                return None
            return bool(verdict.get("claims_unexecuted"))
        except Exception:  # noqa: BLE001 — a claim check must never break task reporting
            log.warning("task report claim check failed", exc_info=True)
            return None

    def _answer_subquestions(self, user_message: str, transcript: str, context_view: str,
                             gathered: List[Dict[str, Any]], model: str,
                             subquestions: List[str],
                             native_blocks: Optional[List[Dict[str, Any]]] = None,
                             reply_directive: Optional[str] = None,
                             rep_preamble: Optional[str] = None) -> str:
        subs = [s for s in subquestions if s][: self.cfg.max_subquestions]
        if len(subs) < 2:
            return self._grounded_answer(user_message, transcript, context_view, gathered, model,
                                         False, native_blocks=native_blocks,
                                         rep_preamble=rep_preamble,
                                         reply_directive=reply_directive)
        ground = _grounding_block(context_view, gathered, False)
        # Same contract as _grounded_answer: a per-turn reply directive rides on the system prompt.
        sub_system = (f"{REPLY_VOICE_SYSTEM}\n\n{reply_directive}" if reply_directive
                      else REPLY_VOICE_SYSTEM)

        def answer_one(sub: str) -> Optional[Dict[str, str]]:
            try:
                # Each sub-question gets the native image blocks too, so any visual sub-question
                # can see the image (the answering model is vision-capable on this path).
                focus = f"Focus ONLY on this sub-question, grounded in the context above:\n\n{sub}"
                if native_blocks:
                    focus_content: List[Dict[str, Any]] = list(native_blocks)
                    focus_content.append({"type": "text", "text": focus})
                    sub_msg = {"role": "user", "content": focus_content}
                else:
                    sub_msg = {"role": "user", "content": focus}
                # The per-turn rep_preamble (persona + plan-mode addenda such as the free-plan
                # documentation-mode refusal) leads, exactly as on the single-answer path.
                msgs = [{"role": "user", "content": ground}, sub_msg]
                if rep_preamble:
                    msgs.insert(0, {"role": "user", "content": rep_preamble})
                provider = self.get_provider_for_model(model)
                # A single surviving sub-answer is returned to them verbatim (see below), so each one
                # is written under the same voice contract as a whole reply.
                return {"q": sub, "a": answer_with_reasoning(provider, msgs, model=model,
                                                             step=STEP_REPLY, system=sub_system)}
            except Exception as e:  # noqa: BLE001
                log.warning(f"Sub-question answer generation failed: {type(e).__name__}: {e}", exc_info=True)
                return None

        workers = min(self.cfg.max_parallel, len(subs))
        out: List[Optional[Dict[str, str]]] = [None] * len(subs)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(answer_one, s): i for i, s in enumerate(subs)}
            for f in futs:
                try:
                    out[futs[f]] = f.result()
                except Exception as e:  # noqa: BLE001
                    log.warning(f"Sub-question result collection failed: {type(e).__name__}: {e}", exc_info=True)
                    out[futs[f]] = None
        ok = [a for a in out if a and (a.get("a") or "").strip()]
        if not ok:
            return self._grounded_answer(user_message, transcript, context_view, gathered, model,
                                         False, native_blocks=native_blocks,
                                         rep_preamble=rep_preamble,
                                         reply_directive=reply_directive)
        if len(ok) == 1:
            return ok[0]["a"]
        merged = "\n\n".join(f"SUB-QUESTION: {a['q']}\nANSWER: {a['a']}" for a in ok)
        try:
            # The old wording here opened with "The user asked: ...", which handed the model a
            # third-person frame and invited it to narrate the split back ("The user asked about X,
            # here is the merged answer"). Address it as their message, and say the split is internal.
            merge_msgs: List[Dict[str, Any]] = (
                [{"role": "user", "content": rep_preamble}] if rep_preamble else [])
            return answer_with_reasoning(
                self.provider,
                merge_msgs + [{"role": "user", "content": (
                    f"Their message was:\n\n{user_message}\n\nYou answered its independent parts "
                    "below. The split is INTERNAL scaffolding: merge the parts into ONE coherent, "
                    "non-repetitive reply written straight to them, and never mention the split, "
                    "the sub-questions, or the headings below.\n\n" + merged)}],
                model=model,
                step=STEP_REPLY,
                system=sub_system,     # the merge writes the reply too, so it obeys the directive
            )
        except Exception:  # noqa: BLE001
            return "\n\n".join(a["a"] for a in ok)
