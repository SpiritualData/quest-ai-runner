"""Understanding the input: decision lookups, goal condition, clarify and confirm.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, Optional, Tuple

from .auxiliary_prompts import (
    DERIVE_GOAL_CONDITION_PROMPT,
    GOAL_CONDITION_SYSTEM,
    RESOLVE_REQUEST_PROMPT,
    _CONDENSE_DECISION_PROMPT,
)
from .adapters import (
    EVENT_DECISION,
    EVENT_DONE,
    EVENT_MILESTONE,
    Escalation,
    PlanDecision,
    ProgressEvent,
    STEP_SUMMARIZE,
    STEP_UNDERSTAND,
    answer_with_reasoning,
    parse_deadline,
)
from .guard import ExecutionRecord
from .orchestrator_decisions import (
    _ACK_PHRASES,
    _ANAPHORA_RE,
    _CONCISE_DECISION_LIMIT,
    _clarify_question_text,
    _format_now_block,
    is_small_talk,
    parse_goal_condition_reply,
)
from .orchestrator_constants import log
from .orchestrator_turn import _Emitter
from .orchestrator_types import OrchestratorResult


class UnderstandingMixin:
    """Understanding the input: decision lookups, goal condition, clarify and confirm."""

    # --- clarify (user selection/clarification) --------------------------------

    def _concise_decision_summary(self, text: str) -> str:
        """Return a concise summary safe to store as a Quest goal CONDITION.

        A decision summary becomes a goal condition (a short done-standard), so raw text (a verbose
        planner question, a brief, dumped analysis) must not go there. A short summary passes through
        unchanged; a long one is condensed by a cheap LLM call into a one or two sentence ask, with a
        hard-truncation fallback if the model call fails. Never raises.
        """
        s = (text or "").strip()
        if len(s) <= _CONCISE_DECISION_LIMIT:
            return s
        try:
            model = self.registry.resolve_tier(self.cfg.planner_tier)
            out = answer_with_reasoning(
                self.provider,
                [{"role": "user", "content": _CONDENSE_DECISION_PROMPT.format(text=s[:6000])}],
                model=model,
                step=STEP_SUMMARIZE,
            )
            if isinstance(out, str) and out.strip():
                return out.strip()[:_CONCISE_DECISION_LIMIT]
        except Exception:  # noqa: BLE001 — condensing must never break an escalation
            pass
        return s[:_CONCISE_DECISION_LIMIT].rstrip() + " [...]"

    def quest_open_decision_ids(self, quest_id: Optional[str]) -> FrozenSet[str]:
        """Best-effort snapshot of ``quest_id``'s currently open decision ids, or empty.

        Used to recover a decision a deep worker created directly against the consumer's API (out
        of band from this process's own escalation sink) but failed to report back via the
        ``QAR-ESCALATED`` marker: diffing a before/after snapshot around the run finds it without
        depending on the worker's own text. Empty when there is no quest_id, no escalation sink is
        wired, or the wired sink does not support the lookup (an ordinary ``EscalationSink``
        implementing only ``escalate``) -- never raises.
        """
        if not quest_id or self.escalation is None:
            return frozenset()
        probe = getattr(self.escalation, "open_decision_ids_for_quest", None)
        if not callable(probe):
            return frozenset()
        try:
            return frozenset(probe(quest_id) or ())
        except Exception:  # noqa: BLE001 — a failed probe must never break the run
            return frozenset()

    def find_new_decision_id(self, quest_id: Optional[str], before: FrozenSet[str]) -> Optional[str]:
        """A decision id that appeared for ``quest_id`` since ``before`` was captured, or None.

        Orphan-recovery fallback, only consulted when a deep run's OWN result carried no
        decision_id (the ``QAR-ESCALATED`` marker path already found nothing). Picks the single
        new id; if more than one appeared in the same run window, logs it and takes the first —
        attributing a specific one to THIS run precisely is what the marker convention exists to
        avoid needing, and any orphan surfacing to a human beats one staying invisible.
        """
        after = self.quest_open_decision_ids(quest_id)
        new_ids = after - before
        if not new_ids:
            return None
        if len(new_ids) > 1:
            log.warning("multiple new open decisions appeared for quest %s during one deep run "
                       "with no QAR-ESCALATED marker (%s); attaching the first",
                       quest_id, sorted(new_ids))
        return sorted(new_ids)[0]

    # --- STEP 1: User Input Understanding ------------------------------------

    def _needs_context_to_understand(self, msg: str) -> bool:
        """Cheap, NO-LLM check: does this message lean on conversation context to be understood?

        Returns True for short/anaphoric inputs that can't stand alone — a pure acknowledgement
        ("ok", "go ahead"), a very short message (<= ~5 words), or one that leans on a pronoun /
        anaphor ("it", "that", "the first one", "as we discussed") WITHOUT a concrete noun to anchor
        it. CONSERVATIVE by design: when unsure, return False so a self-contained input skips the
        LLM hop entirely and adds zero latency. Never raises."""
        try:
            s = (msg or "").strip().lower()
            if not s:
                return False
            # Pure acknowledgement / deferral (whole message is one of the ack phrases): needs context.
            if s.rstrip(".!?") in _ACK_PHRASES:
                return True
            words = s.split()
            # Very short messages can't carry a self-contained instruction on their own.
            if len(words) <= 5:
                return True
            # Longer messages: only treat as context-dependent when they lean on an anaphor AND have
            # no concrete noun-like anchor. We approximate "concrete noun" as a token > 3 chars that
            # is not itself a stopword/anaphor/ack word — conservative, so we DON'T fire on a request
            # that names its own subject (e.g. "update the pricing docs to mention the new tier").
            if _ANAPHORA_RE.search(s):
                _generic = _ACK_PHRASES | {
                    "the", "a", "an", "and", "or", "but", "to", "of", "for", "with", "please",
                    "can", "you", "could", "would", "will", "should", "it", "its", "that", "this",
                    "those", "these", "them", "they", "one", "first", "second", "last", "other",
                    "previous", "as", "we", "discussed", "like", "before", "same", "now", "then",
                }
                has_concrete_noun = any(
                    len(w.strip(".,!?:;\"'")) > 3 and w.strip(".,!?:;\"'") not in _generic
                    for w in words
                )
                return not has_concrete_noun
            return False
        except Exception:  # noqa: BLE001 — the gate must never break the run
            return False

    def _understand_input(self, user_message: str, conv_id: str, conv_scope: Dict[str, Any],
                          emit: "_Emitter"):
        """Resolve a short/anaphoric ``user_message`` into a self-contained GOAL CONDITION using the
        wired ``conversation_store``. Returns ``(goal_condition, conv_ctx_text, clarify_or_None)``:

          * ``goal_condition``  — the rewritten self-contained instruction (defaults to the raw
            message when resolution does not produce a better one).
          * ``conv_ctx_text``   — the conversation-context text actually pulled (current slice plus
            any related slice), so the caller can inject it into ``context_view``.
          * ``clarify_or_None`` — a short question to ask the user, or None.

        Iterative context expansion — stops as soon as the LLM resolves the message:
          1. Last 1 exchange (cheap — covers the vast majority of follow-up messages).
          2. Last 3 exchanges (if MORE_CONTEXT_NEEDED — catches multi-turn threads).
          3. Related past sessions (if still MORE_CONTEXT_NEEDED — cross-session recall).
          4. Escalate to CLARIFY if still unresolved.

        At most THREE LLM calls. Never raises (the caller also guards)."""
        store = self.conversation_store
        model = self.registry.resolve_tier(self.cfg.planner_tier)

        def _resolve(ctx_text: str) -> str:
            prompt = RESOLVE_REQUEST_PROMPT.format(
                conv_context=ctx_text or "=== CURRENT CONVERSATION ===\n(no prior conversation available)",
                user_message=user_message)
            try:
                # Same contract as _derive_goal_condition: this call resolves a request into a
                # done-standard, it does not talk to anyone. Without it, a model handed a bare
                # message answers it, and that answer gets labelled "Understood as: ...".
                out = answer_with_reasoning(
                    self.provider, [{"role": "user", "content": prompt}], model=model,
                    step=STEP_UNDERSTAND, system=GOAL_CONDITION_SYSTEM)
            except Exception:  # noqa: BLE001 — provider error degrades to "no resolution"
                return ""
            return (out or "").strip()

        def _current_block(recent_turns: int) -> str:
            cur = store.current_slice(conv_id, user_message, recent_turns=recent_turns)
            cur_text = (cur.text or "") if cur is not None else ""
            # Label the CURRENT conversation distinctly from any OTHER conversations pulled on
            # widening, so the resolver only borrows a referent when it clearly continues it
            # (a bare "do it" / "the third one" must CLARIFY, never grab an unrelated list).
            return f"=== CURRENT CONVERSATION ===\n{cur_text}" if cur_text else ""

        # Step 1: last 1 exchange — covers most follow-ups ("ok do it", "yes", "repeat that").
        conv_ctx_text = _current_block(recent_turns=1)
        reply = _resolve(conv_ctx_text)

        if reply == "MORE_CONTEXT_NEEDED":
            # Step 2: expand to last 3 exchanges — covers multi-turn threads.
            conv_ctx_text = _current_block(recent_turns=3)
            reply = _resolve(conv_ctx_text)

        if reply == "MORE_CONTEXT_NEEDED":
            # Step 3: pull related past sessions alongside the expanded current slice.
            try:
                rel = store.related_slices(user_message, conv_scope, exclude_conv_id=conv_id)
            except Exception:  # noqa: BLE001
                rel = None
            if rel is not None and rel.text:
                related_block = f"=== OTHER PAST CONVERSATIONS (may be unrelated) ===\n{rel.text}"
                conv_ctx_text = (conv_ctx_text + "\n\n" + related_block if conv_ctx_text
                                 else related_block)
            reply = _resolve(conv_ctx_text)

        if reply == "MORE_CONTEXT_NEEDED" or reply.startswith("CLARIFY:"):
            if reply.startswith("CLARIFY:"):
                q = reply[len("CLARIFY:"):].strip() or "Could you clarify what you'd like me to do?"
            else:
                q = "Could you clarify what you'd like me to do?"
            return user_message, conv_ctx_text, q

        # A successful resolution is the goal condition; fall back to the raw message if empty.
        goal_condition = reply if reply else user_message
        return goal_condition, conv_ctx_text, None

    def _derive_goal_condition(
        self, user_message: str, *, now: Optional[str] = None
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Derive a concise, CHECKABLE done-standard for a SELF-CONTAINED input (Fix 13).

        Unlike ``_understand_input`` (the context-FETCH path, gated by
        ``_needs_context_to_understand`` and only run for anaphoric/short follow-ups), this runs for
        EVERY turn that did NOT need conversation context -- goal-condition ESTABLISHMENT is a
        separate concern from context-fetching, and now always happens. Uses ONE cheap-tier LLM call
        (no conversation history needed, since the message is already self-contained) to restate the
        message as a concrete done-standard, mirroring the deep planner's own ``goal`` field in
        spirit: "the SHORT, CHECKABLE DONE-STANDARD... the single condition that means the work is
        COMPLETE", not just a restatement.

        Query-aware retrieval routing (spec v3, work package C): the SAME reply may ALSO carry
        optional structured retrieval constraints (time_range/topic_terms/actor/content_kind), parsed
        by ``parse_goal_condition_reply`` -- no new LLM call, just more parsed from the one call this
        already makes. ``now`` (optional ISO date/datetime string) resolves relative date expressions
        in the message ("Wednesday", "last week") against a real "today"; see ``_format_now_block``.
        Returns ``(goal_condition, constraints)``; ``constraints`` is None exactly when nothing in
        the message calls for filtering -- today's behavior, byte for byte, for ``goal_condition``.

        Fails safe: any exception, or an empty/unusable reply, degrades to the raw ``user_message``
        unchanged with no constraints (today's fallback), and never breaks the run. Deliberately uses
        a CHEAP tier ("fast", never "best"): this adds one LLM round trip to every turn that reaches
        here, so it must stay fast and inexpensive.
        """
        # A bare greeting/thanks has no request to resolve. Asking a cheap model to restate it is how
        # "Hello" came back as "Hello. How can I help you with your Quests today?" and ended up
        # echoed above the reply. Skip the call: the message IS its own done-standard.
        if is_small_talk(user_message):
            return user_message, None
        # The gist, never the whole brief: a fast judgment call does not need a 110K-character
        # composed brief, and when it fails the fallback becomes the retrieval query, which must
        # not be the whole brief either (``prompt_budget.decision_excerpt``).
        from .prompt_budget import decision_excerpt
        gist = decision_excerpt(user_message)
        try:
            model = self.registry.resolve_tier("fast")
            prompt = DERIVE_GOAL_CONDITION_PROMPT.format(
                user_message=gist, now_block=_format_now_block(now))
            out = answer_with_reasoning(
                self.provider, [{"role": "user", "content": prompt}], model=model,
                step=STEP_UNDERSTAND, system=GOAL_CONDITION_SYSTEM)
            goal_condition, constraints = parse_goal_condition_reply(out or "")
            return (goal_condition or gist), constraints
        except Exception:  # noqa: BLE001 — must never break the run
            return gist, None

    def _run_clarify(self, plan: PlanDecision, *, quest_id: Optional[str] = None,
                     emit: Optional[_Emitter] = None) -> OrchestratorResult:
        """Surface user clarification/selection need as a decision request.

        Creates a decision-request with the question and options (if any) so the user can
        respond on the frontend or terminal. Returns with kind="confirm" to trigger UI.
        """
        clarif = plan.clarification or {}
        question = (clarif.get("question") or "").strip() or "Need your input to proceed"
        options = clarif.get("options") or []
        allow_free = clarif.get("allow_free_input", False)
        # Same rendering the brainstorm (non-escalating) path uses, so the user is asked the same
        # question whichever way it reaches them.
        question_with_opts = _clarify_question_text(plan)

        decision_id = None
        if self.escalation is not None:
            try:
                decision_id = self.escalation.escalate(Escalation(
                    # Condense to a concise done-standard: a decision summary is stored as a goal
                    # CONDITION, never a place to dump raw text.
                    summary=self._concise_decision_summary(question_with_opts),
                    kind=plan.confirm_kind or ("clarify" if options or allow_free else "approve"),
                    quest_id=quest_id,
                    default_on_silence=plan.confirm_default_on_silence or "hold",
                    deadline=parse_deadline(plan.confirm_deadline),
                    review_url=plan.confirm_review_url))
            except Exception:  # noqa: BLE001
                decision_id = None

        if emit is not None:
            emit.emit(ProgressEvent(type=EVENT_MILESTONE,
                                    text=f"Clarification needed: {question}"))

        return OrchestratorResult(kind="confirm", question=question_with_opts,
                                  decision_id=decision_id, rationale=plan.rationale)

    # --- confirm -------------------------------------------------------------

    def _run_confirm(self, plan: PlanDecision, *, quest_id: Optional[str]) -> OrchestratorResult:
        question = (plan.confirm_question
                    or "Before I act on this — can you confirm you'd like me to go ahead?").strip()
        decision_id = None
        if self.escalation is not None:
            try:
                decision_id = self.escalation.escalate(Escalation(
                    # Condense long questions: the summary is stored as a goal CONDITION.
                    summary=self._concise_decision_summary(question),
                    kind=plan.confirm_kind or "approve", quest_id=quest_id,
                    default_on_silence=plan.confirm_default_on_silence or "hold",
                    deadline=parse_deadline(plan.confirm_deadline),
                    review_url=plan.confirm_review_url))
            except Exception:  # noqa: BLE001 — escalation failure still returns the question
                decision_id = None
        return OrchestratorResult(kind="confirm", question=question, decision_id=decision_id,
                                  rationale=plan.rationale)

    def _finish_understanding_clarify(
        self, res: OrchestratorResult, *, emit: "_Emitter", exec_record: "ExecutionRecord",
        user_message: str, ctx_meta: Dict[str, Any]) -> OrchestratorResult:
        """Terminate the turn on a STEP 1 (understanding) CLARIFY, mirroring the confirm branch of
        ``run``'s ``finish``: attach the execution record, emit the decision + done events, and run
        the best-effort ContextAssembler write-back. The planner loop is NOT run. Never raises from
        the write-back (it is wrapped). Kept tiny and parallel to ``finish`` so a clarify produced by
        understanding reports IDENTICALLY to a planner-produced confirm (executor → needs_you)."""
        res.steps = 0
        res.gathered = []
        res.execution_record = exec_record
        if self.context_assembler is not None:
            try:
                self.context_assembler.record(
                    user_message,
                    {"kind": res.kind, "steps": 0, "files": [], "response": res.question,
                     **(ctx_meta or {})},
                )
            except Exception:  # noqa: BLE001 — write-back must never break the run
                pass
        emit.emit(ProgressEvent(type=EVENT_DECISION, text=res.question,
                                decision_id=res.decision_id, result_kind="confirm"))
        emit.emit(ProgressEvent(type=EVENT_DONE, result_kind=res.kind, step=0))
        return res
