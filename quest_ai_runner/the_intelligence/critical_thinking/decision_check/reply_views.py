"""The reply's three views for the hub's Decision Check: what the turn read, what it did, and whom it may ask.

Role: before a chat reply is sent, small independent checks (the hub's agreement check, each on the cheap tier) hold it to the record of the turn, one kind of evidence each, so a small model is
reliable on it: (1) a statement that something was read or looked up against the reads the turn actually made; (2) a statement that a change was made against the executed-action record the
runner wrote from the tools' own receipts; (3) a question or request put to a person against who is in the conversation. The checks run in parallel; a span is repaired only when an independent
call confirms it fails, and only that span is rewritten or removed, with the evidence that contradicts it. Whether a reply denies work that landed, and whether a question to the person present
is asked in the reply, are decided elsewhere (``keep_landed_work_reported``, ``file_blocked_ask``); this adds the two checks that held nothing to the record before: claims against reads and
claimed actions against executed ones. Off unless ``OrchestratorConfig.reply_check`` is set. Never raises: any failure sends the reply as it was.
"""
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

from hubofhubs.api import STEP_JUDGE, AgreementCheck, answer_with_reasoning
from hubofhubs.api import UNCONFIRMED_REPLY as UNCONFIRMED_DRAFT_TEXT

log = logging.getLogger("quest-ai-runner.reply-check")

READS_VIEW = ("A statement that something was read, looked up, opened or found in a source must be supported by the reads listed in the evidence. "
              "A statement of any other kind (an action done, a plan, a question, advice) is not this task.")
ACTIONS_VIEW = ("A statement that a change was made or an action done (added, created, moved, sent, scheduled, set up), as a fact in the past or present, must be supported by an executed action "
                "in the evidence. A plan, an offer, a read and a question are not this task.")
ASKS_VIEW = ("A question or request addressed to a named person who is not listed as present in the evidence fails this task. A question to a person who is present, "
             "and every statement that is not a question or request, does not fail it. A sentence that says who does or owns a step is a statement about them, not a request "
             "addressed to them, and does not fail.")
FACTS_VIEW = ("A statement of fact about the person's quest, work, people, dates, figures or events (something happened, was missed, is due, stands at a value) must be "
              "supported by the reads listed in the evidence. Statements inside a drafted text the reply hands over (a script, message, post or notes) count too: that some "
              "work was done or a result came out, and what a product, service or organisation is, does, has or offers. A plan, an offer, a question, advice, a call to "
              "action and general knowledge are not this task.")
CLAIMS_VIEW = ("A statement in this drafted text that some work was done or a result came out, or about what a product, service, person or organisation is, does, has or offers, "
               "must be supported by the evidence. What the text asks its reader to do, a question, a next step, a plan, and its form and tone are not this task.")
EVIDENCE_CHARS = 6000
GROUNDING_CHARS = 16000   # the reads and facts views see what the writer saw: the turn's reads and its context (a status snapshot, the quest's own state), not a cut of it
NONE_TEXT = "(none)"
# At most this many check calls in flight. Measured on the production replay (2026-11-08): with 8 at once plus the turn's own calls the provider answered
# "concurrent connections exceeded" (429) and its retries put 6 to 19 s on single calls, the bulk of the check's added time.
REPLY_CHECK_PARALLEL = 4


def clip(text: Any, limit: int = EVIDENCE_CHARS) -> str:
    raw = str(text or "").strip()
    return raw if len(raw) <= limit else raw[:limit - 1].rstrip() + "…"


def reads_text(gathered: Sequence[Any]) -> str:
    """The turn's reads as plain text: each observation's source and text."""
    lines: List[str] = []
    for item in gathered or ():
        if isinstance(item, dict):
            lines.append(f"- {item.get('locator') or item.get('source') or 'read'}: {clip(item.get('text') or item.get('content') or json.dumps(item, default=str)[:600], 800)}")
        else:
            lines.append(f"- {clip(item, 800)}")
    return "\n".join(lines)


def present_text(participants: Sequence[Any]) -> str:
    """Who is in the conversation: the person typing to us, and the participants the turn lists by name."""
    names = [str(p.get("name") or p.get("user_id") or "").strip() for p in participants or () if isinstance(p, dict)]
    return "the person writing to us" + "".join(f", {n}" for n in names if n)


def reply_binding(reads: str, executed: str, present: str, max_calls: int = 40) -> Dict[str, Any]:
    """The hub's Binding for a reply: four views, each shown only its own evidence."""
    return {"kind": "reply", "max_calls": max_calls, "parallel": REPLY_CHECK_PARALLEL, "views": [
        {"id": "reads", "instruction": READS_VIEW, "evidence": "Reads this turn:\n" + (clip(reads, GROUNDING_CHARS) or NONE_TEXT)},
        {"id": "facts", "instruction": FACTS_VIEW, "evidence": "Reads this turn:\n" + (clip(reads, GROUNDING_CHARS) or NONE_TEXT)},
        {"id": "actions", "instruction": ACTIONS_VIEW, "evidence": "Executed actions this turn:\n" + (clip(executed) or NONE_TEXT)},
        {"id": "asks", "instruction": ASKS_VIEW, "evidence": "Present in this conversation: " + (clip(present, 600) or NONE_TEXT)}]}


def grounded_text(text: str, evidence: str, ask: Callable[[str], str], max_calls: int = 12) -> str:
    """A drafted deliverable (a script, a message, notes) held to the facts it was written from before it is kept anywhere: the hub's Decision Check with one claims view, so
    a claim no fact supports is confirmed by independent calls and rewritten from the evidence or removed with its sentence. The text as it was when the check cannot run."""
    if not (text or "").strip() or ask is None:
        return text
    binding = {"kind": "reply", "max_calls": max_calls, "parallel": REPLY_CHECK_PARALLEL,
               "views": [{"id": "draft_claims", "instruction": CLAIMS_VIEW, "evidence": "Facts the draft was written from:\n" + (clip(evidence, GROUNDING_CHARS) or NONE_TEXT)}]}
    try:
        result = AgreementCheck(plumbing=None).check(text, binding, ask)
    except Exception:  # noqa: BLE001 -- a check must never break the work it checks
        log.warning("draft claim check failed; the draft is kept as written", exc_info=True)
        return text
    log.info("Draft claim check: %s in %s calls: %s", result.get("outcome"), result.get("calls"), result.get("why"))
    fixed = str(result.get("text") or "")
    return fixed if result.get("outcome") == "repaired" and fixed.strip() and fixed != UNCONFIRMED_DRAFT_TEXT else ("" if result.get("outcome") == "repaired" else text)


def model_ask(orch: Any, tier: str) -> Optional[Callable[[str], str]]:
    """The one-argument model call of the cheap tier through the orchestrator's own provider, or None when the tier cannot be resolved."""
    model = orch.registry.resolve_tier(tier)
    if not model:
        return None
    provider = orch.get_provider_for_model(model)
    return lambda prompt: answer_with_reasoning(provider, [{"role": "user", "content": prompt}], model=model, step=STEP_JUDGE) or ""


def keep_mistakes(rows: Sequence[Any], path: str) -> None:
    """Each confirmed span the check removed instead of rewriting, as a mistake record the learning loop reads (``MistakeLog`` at ``path``), and in the log. Never raises."""
    for row in rows:
        log.info("Reply check mistake: %s removed (%s)", str(row.get("wrong"))[:300], row.get("right"))
    if not path or not rows:
        return
    try:
        from hubofhubs.api import Mistake, MistakeLog
        book = MistakeLog(path)
        for row in rows:
            book.add(Mistake(**row))
    except Exception:  # noqa: BLE001 -- keeping a record must never break the turn
        log.warning("reply check mistakes not kept", exc_info=True)


def checked_reply(orch: Any, rs: Any, text: str) -> str:
    """``text`` held to the turn's reads, executed actions and participants; unchanged when the check is off, has nothing to hold it to, or fails."""
    cfg = orch.cfg
    if not getattr(cfg, "reply_check", False) or not (text or "").strip():
        return text
    try:
        from quest_ai_runner.the_intelligence.mind.context_intake.planner_context import answer_evidence
        from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_text import tool_landed_account
        ask = model_ask(orch, getattr(cfg, "reply_check_tier", "fast"))
        if ask is None:
            return text
        meta = getattr(rs, "_ctx_meta", None) or {}
        # What the reply may rest on is what its writer was shown: the turn's reads and the context it was written from (a status snapshot, the quest's own state).
        # Held to the reads alone, a true sentence taken from that context was removed as unsupported.
        context = str(getattr(rs, "context_view", "") or "").strip()
        grounding = reads_text(answer_evidence(getattr(rs, "gathered", None) or [])) + (f"\n- the context the reply was written from: {context}" if context else "")
        binding = reply_binding(grounding, tool_landed_account(getattr(rs, "exec_record", None)),
                                present_text(meta.get("participants")), int(getattr(cfg, "reply_check_max_calls", 40)))
        result = AgreementCheck(plumbing=None).check(text, binding, ask)
        rs.reply_check = {k: result.get(k) for k in ("outcome", "calls", "agreement", "why")}
        keep_mistakes(result.get("mistakes") or [], str(getattr(cfg, "reply_check_mistakes_path", "") or ""))
        log.info("Reply check: %s in %s calls (agreement %s): %s", result.get("outcome"), result.get("calls"), result.get("agreement"), result.get("why"))
        fixed = str(result.get("text") or "")
        return fixed if result.get("outcome") == "repaired" and fixed.strip() else text
    except Exception:  # noqa: BLE001 -- the check must never break the turn
        log.warning("reply check failed; the reply is sent as it was", exc_info=True)
        return text
