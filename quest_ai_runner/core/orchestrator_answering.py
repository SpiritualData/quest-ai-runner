"""Composing answers: a mixin of ``Orchestrator`` (see ``orchestrator.py``) over the hub's ``hubofhubs.orchestrate.answer``.

The hub holds the flow (grounded answer, sub-question fan-out, post-work synthesis). QAR supplies the text and renderers as an
``AnswerKit``: the reply voice, the synthesis prompts (``core/auxiliary_prompts.py``), the grounding blocks
(``core/orchestrator_events.py``) and the sentences the flow falls back to. The old underscore method names stay on the class.
"""
from __future__ import annotations

from typing import Any

from hubofhubs.orchestrate.answer import AnswerKit, AnsweringMixin as HubAnsweringMixin
from .auxiliary_prompts import (
    REPLY_VOICE_SYSTEM,
    SYNTHESIZE_AFTER_DEEP_PROMPT,
    SYNTHESIZE_TASK_REPORT_PROMPT,
    SYNTHESIZE_AFTER_QUEUED_PROMPT,
)
from .prompt_layers import compose_layers
from .orchestrator_events import _grounding_block, grounding_answer_tail, grounding_context_layer
from .orchestrator_capabilities import provider_call_accepts_layers
from .orchestrator_constants import log


def layered_blocks(persona: str, context: str, tail: str) -> Any:
    return compose_layers(persona=persona, context=context, tail=tail).blocks()


QAR_ANSWER_KIT = AnswerKit(
    reply_voice_system=REPLY_VOICE_SYSTEM,
    synthesize_after_deep=SYNTHESIZE_AFTER_DEEP_PROMPT,
    synthesize_task_report=SYNTHESIZE_TASK_REPORT_PROMPT,
    synthesize_after_queued=SYNTHESIZE_AFTER_QUEUED_PROMPT,
    # Each sub-question gets the context above and nothing else to answer.
    subquestion_focus="Focus ONLY on this sub-question, grounded in the context above:\n\n{sub}",
    # The old wording here opened with "The user asked: ...", which handed the model a third-person frame and invited it to
    # narrate the split back. Address it as their message, and say the split is internal.
    merge_prompt=("Their message was:\n\n{message}\n\nYou answered its independent parts "
                  "below. The split is INTERNAL scaffolding: merge the parts into ONE coherent, "
                  "non-repetitive reply written straight to them, and never mention the split, "
                  "the sub-questions, or the headings below.\n\n{merged}"),
    merged_part="SUB-QUESTION: {q}\nANSWER: {a}",
    done_standard_line=("- The DONE-STANDARD for this work is: {standard}\n"
                        "  The reply must show it met, or say plainly that it is not.\n"),
    queued_note=("I have queued this work to run in the background and will report back in this "
                 "conversation when it finishes."),
    report_goal="Honestly report the outcome of: {request}",
    none_marker="(none)",
    handoff_marker="(hand-off confirmed)",
    grounding_block=_grounding_block,
    grounding_tail=grounding_answer_tail,
    grounding_layer=grounding_context_layer,
    layered_blocks=layered_blocks,
    accepts_layers=provider_call_accepts_layers,
    log=log,
)


class AnsweringMixin(HubAnsweringMixin):
    """Composing answers: the hub's mixin with QAR's kit."""

    answer_kit = QAR_ANSWER_KIT

    _answer_model = HubAnsweringMixin.answer_model
    _grounded_answer = HubAnsweringMixin.grounded_answer
    _synthesize_after_deep = HubAnsweringMixin.synthesize_after_deep
    _synthesize_after_queued = HubAnsweringMixin.synthesize_after_queued
    _answer_subquestions = HubAnsweringMixin.answer_subquestions

    def verify_goal(self, *args: Any, **kwargs: Any) -> Any:
        """The done-check, looked up at call time so a test or a replay that replaces ``_verify_goal`` is honoured."""
        return self._verify_goal(*args, **kwargs)
