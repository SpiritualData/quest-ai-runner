"""Text helpers for answers and future-context asks: clipping, parsing, normalizing, display.

Split out of ``orchestrator.py`` by definition; ``orchestrator`` re-exports every name here.
"""
from __future__ import annotations

from typing import Any, List, Optional

from .adapters_values import DeepResult, FUTURE_CONTEXT_VIA_FIELD, FUTURE_CONTEXT_VIA_OUTPUT
from .auxiliary_prompts import FUTURE_CONTEXT_DELIMITER


# NOTE (do not reintroduce): there was once a `_confirm_is_redundant` gate here, keyed on a
# `_CONFIRM_FORK_MARKERS` denylist, that auto-executed a planner-originated confirm (skipping the
# human fork) whenever neither the user's request NOR THE PLANNER'S OWN CONFIRM QUESTION contained
# a risky keyword (delete/send/pay/deploy/…). It was removed deliberately. Gating a human-in-the-
# loop safety decision on substring matches against free-form MODEL OUTPUT is exactly the brittle
# pattern QAR must not use: the planner phrases its confirm question arbitrarily, so any fixed
# denylist silently leaks (share/invite/grant/message/gift/transfer/… all slipped through and
# auto-executed), and an allowlist is no better — both make safety depend on the model happening
# to use a word the code anticipated. When the planner decides an act needs confirmation, HONOR
# that decision; do not second-guess it with keyword logic over the planner's text. If the planner
# over-asks for confirmation, fix that in the PLANNER (its prompt/instructions), not with a
# post-hoc keyword filter on its output. See CLAUDE.md ("Do not gate decisions on keywords in AI
# output") and the qar-playbook.


def ellipsize(text: str, limit: int) -> str:
    """``text`` on one line, cut to ``limit`` characters with a trailing ellipsis when it is longer.

    The cut falls on a word boundary when one is near the end, so a title or a goal is not chopped
    mid-word, and the punctuation the cut leaves behind (a comma, a colon) goes with the cut-off words.
    """
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[: max(limit - 1, 1)]
    after = text[len(cut)]
    if not (after.isspace() or after in ",;:.!?)"):   # the cut is inside a word: back up to the word before
        space = cut.rfind(" ")
        if space >= limit * 0.6:
            cut = cut[:space]
    return cut.rstrip(" ,;:-\u2013") + "…"


def sentence(text: str) -> str:
    """``text`` as a status sentence: ended with a full stop unless it already ends in one, in an ellipsis or in
    other closing punctuation."""
    text = text.rstrip()
    return text if not text or text.endswith((".", "…", "!", "?")) else text + "."


def clip_head_and_tail(text: str, limit: int) -> str:
    """``text`` cut to about ``limit`` chars keeping its START and its END. A queued task's brief
    opens with the standing request and ENDS with what the person just said; a head-only cut
    judged the brief on everything except the newest words."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 5
    tail = limit - head
    return text[:head].rstrip() + "\n[...]\n" + text[-tail:].lstrip()


def _answer_describes_unexecuted_work(text: Optional[str]) -> bool:
    """Check if answer contains EXECUTABLE work that should've been done.

    Returns True ONLY if the unexecuted work is something the AI CAN do.
    Returns False if work is user-dependent (needs input, confirmation, etc).

    Executable: "I need to update code", "I should modify X", etc.
    User-dependent: "I need your input", "Please provide", etc. (OK to describe).
    """
    if not text or not text.strip():
        return False
    try:
        import re
        # Patterns for work AI CAN execute (executable patterns)
        # Match "I/we/you need to", "the system needs to", "the code needs to", etc.
        executable = [
            # Explicit inability confession: "I cannot execute this task", "I can't run commands
            # or edit files here" — the answer itself admits the work was not done.
            r"(?:i|we)\s+(?:cannot|can'?t|am\s+not\s+able\s+to|was\s+unable\s+to)\s+"
            r"(?:execute|run|perform|complete|do|apply|write|edit|modify|create)",
            # "The system will need to execute/run/write ..." — deferring the work to
            # an unnamed executor instead of doing it.
            r"the\s+system\s+(?:will\s+)?(?:need|have)s?\s+to\s+"
            r"(?:execute|run|perform|write|update|modify|change|create|apply)",
            r"(?:i|we|the\s+(?:code|system|logic|field|implementation))\s+(?:need|should|must)\s+(?:to\s+)?(?:update|modify|change|fix|add|remove|delete|create|implement|edit)",
            r"(?:i|we)\s+(?:need|should|must)\s+(?:to\s+)?(?:update|edit|modify).{0,30}(?:code|file|logic|field|database|api|endpoint)",
            r"to\s+(?:fix|address|resolve)\s+this,?\s+(?:i|we)\s+(?:need|should|must)",
            r"to\s+resolve\s+this.*(?:i|we)\s+need\s+to\s+ensure",
            # "the fix/solution is to update X" — describing the change instead of applying it.
            r"the\s+(?:fix|solution)\s+is\s+to\s+(?:update|modify|change|fix|add|remove|delete|create|implement|edit)",
            # "this/it needs to be updated", "the code needs updating" — passive describe-not-do.
            r"(?:this|it|the\s+(?:code|logic|field|implementation))\s+(?:needs?|requires?)\s+(?:to\s+be\s+)?(?:updat|modif|chang|fix|add|remov|delet|creat|implement|edit)",
        ]

        # Patterns for work that NEEDS USER INPUT (don't escalate these)
        user_dependent = [
            r"(?:you|your|please)\s+(?:need|should|must|will need|could|would)",
            r"(?:i|we)\s+need\s+(?:your|the user'?s?)\s+(?:input|decision|feedback|confirmation|approval)",
            r"please\s+(?:provide|specify|confirm|decide|clarify|choose)",
            r"requires?\s+(?:your|user|human)\s+(?:input|decision|confirmation|approval)",
        ]

        # Check user-dependent patterns FIRST (higher priority)
        for pattern in user_dependent:
            if re.search(pattern, text, re.IGNORECASE):
                return False  # OK — work needs user input, don't force execution

        # Check executable patterns
        for pattern in executable:
            if re.search(pattern, text, re.IGNORECASE):
                return True  # Should escalate — AI can execute this
    except Exception:  # noqa: BLE001
        pass
    return False


def _parse_future_context(output: Optional[str]) -> str:
    """Extract the worker's FUTURE-CONTEXT section from a DeepResult.output. Returns "" when absent.

    The deep brief instructs the worker to END its output with the
    ``FUTURE_CONTEXT_DELIMITER`` line followed by bullet lines. This slices from the LAST occurrence
    of the delimiter to the end (the last one wins so a worker that echoes the instruction earlier
    does not confuse the parser), strips a trailing code fence, and returns the bullets verbatim.
    Robust to a missing/garbled section: any miss yields "". Never raises.
    """
    if not output or not isinstance(output, str):
        return ""
    try:
        idx = output.rfind(FUTURE_CONTEXT_DELIMITER)
        if idx < 0:
            return ""
        section = output[idx + len(FUTURE_CONTEXT_DELIMITER):]
        # Drop a trailing markdown fence if the worker wrapped the section.
        section = section.replace("```", "")
        return section.strip()
    except Exception:  # noqa: BLE001
        return ""


def _future_context_channel(runner: Any) -> str:
    """Which FUTURE-CONTEXT channel this deep runner declares (see adapters.FUTURE_CONTEXT_VIA_*).

    Read with ``getattr`` so DUCK-TYPED runners (the common case; most consumers never subclass
    ``DeepRunnerBase``) keep working: anything that does not declare a channel is a prose runner and
    gets exactly today's behaviour. Never raises.
    """
    try:
        ch = getattr(runner, "future_context_channel", FUTURE_CONTEXT_VIA_OUTPUT)
        return ch if ch in (FUTURE_CONTEXT_VIA_OUTPUT, FUTURE_CONTEXT_VIA_FIELD) \
            else FUTURE_CONTEXT_VIA_OUTPUT
    except Exception:  # noqa: BLE001 — a weird runner attribute must never break a deep run
        return FUTURE_CONTEXT_VIA_OUTPUT


def _normalize_future_context(res: DeepResult) -> DeepResult:
    """ONE seam, applied to EVERY DeepResult the moment a runner returns it: move the worker's
    FUTURE-CONTEXT bullets OUT of ``output`` and into ``future_context``.

    This is what makes payload corruption impossible by construction rather than by each consumer
    remembering to strip:

      * a FIELD-channel runner (code / JSON / patch generator) already put its bullets in
        ``future_context`` and left ``output`` a clean payload, so this is a no-op for it -- except
        that a worker which ignored the instruction and appended the delimiter anyway ALSO gets
        cleaned here, which is the whole point of doing it centrally;
      * an OUTPUT-channel (prose) runner ended its output with the delimited section: it is parsed
        into ``future_context`` and cut from ``output``.

    After this, ``output`` is the deliverable and ``future_context`` is the ONLY carrier of the
    bullets, so every downstream reader (the async card updater, the UI panel, the consumer's
    payload) reads exactly one place. Mutates and returns the same result object. Never raises.
    """
    try:
        if not isinstance(res, DeepResult):
            return res
        parsed = _parse_future_context(res.output)
        if not (getattr(res, "future_context", "") or "").strip() and parsed:
            res.future_context = parsed
        if parsed:
            # The delimiter is present in the payload: cut it, whatever the declared channel.
            res.output = _strip_future_context(res.output)
    except Exception:  # noqa: BLE001 — normalization must never break a deep run
        pass
    return res


def _deep_future_context(result: Any) -> str:
    """The FUTURE-CONTEXT bullets of one deep result, from the structured field with a fallback to
    parsing the output.

    The field is authoritative (``_normalize_future_context`` fills it at the runner seam). The
    parse fallback covers a DeepResult built OUTSIDE that seam (e.g. a consumer that constructs one
    directly, or an older runner reflected back through a queue), so the card updater keeps learning
    in those paths too. Never raises.
    """
    try:
        fc = (getattr(result, "future_context", "") or "").strip()
        return fc or _parse_future_context(getattr(result, "output", None))
    except Exception:  # noqa: BLE001
        return ""


def _strip_future_context(output: Optional[str]) -> str:
    """Remove the worker's FUTURE-CONTEXT section from output destined for the USER.

    An OUTPUT-channel deep brief asks the worker to END its output with the
    ``FUTURE_CONTEXT_DELIMITER`` line plus bullets, which the async card updater learns from. That
    section is internal plumbing for learning, NOT part of the deliverable, so it must never reach
    the user. This cuts from the LAST delimiter occurrence to the end (symmetric with
    ``_parse_future_context``, which reads from that same point) and trims trailing whitespace.
    Returns the output unchanged when the delimiter is absent.

    It is applied ONCE, at the runner seam, by ``_normalize_future_context`` -- so ``DeepResult.output``
    is already clean everywhere downstream. The remaining calls on the emit paths are a deliberate
    belt-and-braces net for a DeepResult that never passed through that seam. Never raises.
    """
    if not output or not isinstance(output, str):
        return output or ""
    try:
        idx = output.rfind(FUTURE_CONTEXT_DELIMITER)
        if idx < 0:
            return output
        return output[:idx].rstrip()
    except Exception:  # noqa: BLE001
        return output


# The lead line for a deep result the verifier did NOT confirm, whose own receipts say it changed
# nothing. Written by CODE, never by a model, and placed BEFORE the runner's own text so a reader
# cannot take that text as a record of completed work. Two real failures made this necessary (a
# chat eval, 2026-10-07): a generated program whose write matched no document, and one that wrote
# nothing at all, each ran, each verified not met for exactly that reason, and the turn still told
# the person the change was done. No em dash (brand voice of the first consumer, harmless here).
UNCONFIRMED_NO_CHANGE_LEAD = "Not confirmed as done, and nothing was changed."


def unconfirmed_no_change_text(result: Any) -> str:
    """One deep result's text for the user, with the honesty gate for an unconfirmed no-change run.

    The verdict is STRUCTURED (``DeepResult.met``, written by the goal loop from the verifier's own
    verdict, false also when verification could not run) and so is the receipt
    (``DeepResult.changed_nothing``, set by the runner from its own bookkeeping). When a run both
    failed to verify and reports changing nothing, its output may not stand alone as the turn's
    result: the lead line above goes first. Nothing here reads the WORDS of the output (hard rule
    #3); it is the pair of structured facts that decides. A run whose own text already states that
    nothing changed (``DeepResult.states_no_change``, again a structured field) gets no second
    statement of the same fact. Never raises.
    """
    text = _strip_future_context(getattr(result, "output", None))
    try:
        if text and not getattr(result, "met", False) and \
                getattr(result, "changed_nothing", False) and \
                not getattr(result, "states_no_change", False):
            return f"{UNCONFIRMED_NO_CHANGE_LEAD}\n\n{text}"
    except Exception:  # noqa: BLE001 — the honesty note must never break the reply
        return text
    return text


def _future_context_for_display(results: Any) -> str:
    """Collect the FUTURE-CONTEXT bullets across deep results into ONE display string for the user.

    This is the same section the async card updater learns from (see ``_parse_future_context``),
    surfaced so a consumer can show it as an expandable "what I'll remember for next time" panel on
    a deep-output message. Drops the worker's "(none)" placeholder so an empty section yields "" (the
    UI then shows nothing). Returns "" when there is nothing worth showing. Never raises.
    """
    try:
        parts: List[str] = []
        for d in (results or []):
            section = _deep_future_context(d)
            if not section:
                continue
            # Drop pure "(none)" placeholders; keep real bullets verbatim.
            kept = [ln for ln in section.splitlines()
                    if ln.strip().lower().lstrip("- ").strip() not in ("(none)", "none", "")]
            if kept:
                parts.append("\n".join(kept))
        return "\n".join(parts).strip()
    except Exception:  # noqa: BLE001
        return ""


def _card_update_store(assembler: Any) -> Optional[Any]:
    """Return the underlying card store that exposes the card-update API, or None. Never raises.

    GENERIC capability detection (no hardcoded ``FileContextStore`` type check): an object is
    card-update-capable when it exposes callable ``update_card`` AND ``add_content``. The wired
    ``context_assembler`` may BE such a store, or may be a composite/hybrid that wraps one, so this
    unwraps the known wrapper shapes (a ``CompositeContextAssembler``'s ``_assemblers`` list, a
    ``HybridContextAssembler``'s ``_keyword``/``_vector`` arms) and returns the first capable inner
    store it finds. Returns None when nothing card-update-capable is reachable.
    """
    from hubofhubs.recall.composite_assembler import find_assembler

    def _is_capable(obj: Any) -> bool:
        return bool(obj is not None
                    and callable(getattr(obj, "update_card", None))
                    and callable(getattr(obj, "add_content", None)))

    return find_assembler(assembler, _is_capable)
