"""RepContextAssembler -- what THIS rep tends to consult, and learning more of it each run.

A team's AI reps do not all read the same things. The one who answers funding questions lives in
the spreadsheet and the donor list; the one who handles support lives in the ticket history. That
preference is real, it is learnable from what a run actually reads, and it is per rep -- so it
cannot live in a shared card store, and it is worth nothing unless the next run for that rep sees
it.

This wraps any other ``ContextAssembler`` and adds two halves of one loop:

  * **assemble** prepends the rep's learned ``context_prefs`` (fetched from its Quest AI profile)
    to whatever the inner assembler produced. Only the prefs: the rep's PERSONA and its learned
    corrections are injected into the deep run by the library already, and re-injecting them here
    would double the persona.
  * **record** pushes ONE concise pref back when the run actually consulted a real source
    ("Consults the funding spreadsheet and donor list for this kind of task."), deduped against
    what the rep already has.

WHY IT IS IN THE LIBRARY. It was written once inside a consumer, where it was ~210 lines of
generic machinery -- fetch a profile, render a block, derive consulted sources from a run outcome,
phrase them, dedupe, POST -- with nothing about that org in any of it. A second lane wanting
per-rep context would have had nowhere to find it (hard rule #4 in CLAUDE.md).

WHICH REP a run is for comes from ``runner.personas.current_rep()``, which the persona resolver
stashes on the worker thread as it resolves. In its consumer form this class needed the consumer
to pass an ``on_resolved`` callback and keep that thread-local itself, which was the ONLY reason
that lane could not use the declarative ``personas`` config.

Everything here is best-effort. A missing profile, an unreachable backend, an outcome in an
unexpected shape: each degrades to "no rep block" or "learn nothing", never to a failed run. The
learning half is deliberately conservative -- it pushes only when a run really read something, and
never a restatement of a pref the rep already has.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..core.adapters import AssembledContext, ContextAssemblerBase
from ..runner.personas import current_rep

log = logging.getLogger("quest-ai-runner.rep-context")

# How many distinct sources one learned pref may name. Past three the sentence stops being a
# useful hint about where this rep works and becomes a list.
MAX_LEARNED_SOURCES = 3


def render_context_prefs(prefs: Any) -> str:
    """A rep's learned prefs as a compact "context you tend to consult" block, or ""."""
    if not isinstance(prefs, list):
        return ""
    lines = []
    for pref in prefs:
        text = str(pref.get("text", "") if isinstance(pref, dict) else pref).strip()
        if text:
            lines.append(f"- {text}")
    if not lines:
        return ""
    return "CONTEXT THIS REP TENDS TO CONSULT:\n" + "\n".join(lines)


def sources_consulted(outcome: Dict[str, Any]) -> List[str]:
    """The distinct source paths a run ACTUALLY consulted, from its outcome payload.

    Structural, never a keyword guess: it reports what was really read. Returns [] when nothing
    concrete was consulted, which is the signal to learn nothing at all.
    """
    out: List[str] = []
    seen = set()
    if not isinstance(outcome, dict):
        return out
    for path in (outcome.get("files") or []):
        rel = str(path or "").strip()
        if rel and rel not in seen:
            seen.add(rel)
            out.append(rel)
    # ``steps`` is a per-step dict list in some outcome shapes but a bare STEP COUNT in the
    # orchestrator's own record() payload. Iterating the int raised "'int' object is not iterable"
    # on every push-back for months, so the type is checked rather than assumed.
    steps = outcome.get("steps")
    for step in (steps if isinstance(steps, (list, tuple)) else []):
        if isinstance(step, dict):
            rel = str(step.get("rel_path") or step.get("locator") or "").strip()
            if rel and rel not in seen:
                seen.add(rel)
                out.append(rel)
    return out


def short_source_label(path: str) -> str:
    """A readable label for a consulted path: its leaf name, without extension or separators."""
    leaf = str(path or "").rsplit("/", 1)[-1]
    if "." in leaf:
        leaf = leaf.rsplit(".", 1)[0]
    return leaf.replace("_", " ").replace("-", " ").strip() or str(path or "")


def build_context_pref(sources: List[str]) -> str:
    """A concise factual pref naming what was consulted, or "" when there is nothing to report."""
    labels: List[str] = []
    seen = set()
    for source in sources:
        label = short_source_label(source)
        key = label.lower()
        if label and key not in seen:
            seen.add(key)
            labels.append(label)
        if len(labels) >= MAX_LEARNED_SOURCES:
            break
    if not labels:
        return ""
    if len(labels) == 1:
        listed = labels[0]
    elif len(labels) == 2:
        listed = f"{labels[0]} and {labels[1]}"
    else:
        listed = ", ".join(labels[:-1]) + f", and {labels[-1]}"
    return f"Consults {listed} for this kind of task."


def pref_is_duplicate(pref: str, existing: Any) -> bool:
    """Whether ``pref`` restates one the rep already has, compared on normalized text.

    Substring in EITHER direction, because the near-duplicates that actually occur are one pref
    naming a subset of another's sources.
    """
    def normalize(text: Any) -> str:
        s = str(text or "").lower()
        s = "".join(c if (c.isalnum() or c.isspace()) else " " for c in s)
        return " ".join(s.split())

    candidate = normalize(pref)
    if not candidate:
        return True
    if not isinstance(existing, list):
        return False
    for entry in existing:
        other = normalize(entry.get("text") if isinstance(entry, dict) else entry)
        if not other:
            continue
        if candidate == other or candidate in other or other in candidate:
            return True
    return False


class RepContextAssembler(ContextAssemblerBase):
    """Wraps a ContextAssembler with the current rep's learned context preferences."""

    def __init__(self, inner: ContextAssemblerBase, *, client: Any, team_id: str = "") -> None:
        self._inner = inner
        self._client = client
        self._team_id = team_id

    # --- read half -------------------------------------------------------------------------

    def _rep_block(self) -> str:
        stashed = current_rep()
        if not stashed:
            return ""
        user_id = stashed.get("user_id")
        if not user_id:
            return ""
        task = stashed.get("task") or {}
        team_id = task.get("team_id") or self._team_id
        try:
            profile = self._client.get_ai_profile(user_id, team_id=team_id)
            block = render_context_prefs((profile or {}).get("context_prefs"))
        except Exception as e:  # noqa: BLE001 -- a profile fetch is grounding, not a precondition
            log.info("context_prefs fetch for %s failed (%s), continuing", user_id, e)
            return ""
        if not block:
            return ""
        return ("=== AI REP CONTEXT (what this rep tends to look at) ===\n\n"
                + block + "\n\n=== END AI REP CONTEXT ===")

    def assemble(self, task_text: str,
                 *, meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        try:
            inner = self._inner.assemble(task_text, meta=meta)
        except Exception:  # noqa: BLE001 -- a failing inner assembler still gets the rep block
            inner = AssembledContext()
        block = self._rep_block()
        if not block:
            return inner
        view = block if not inner.context_view else f"{block}\n\n{inner.context_view}"
        return AssembledContext(
            context_view=view,
            model_tier_hint=inner.model_tier_hint,
            card_ids=inner.card_ids,
            stale=inner.stale,
        )

    # --- learning half ---------------------------------------------------------------------

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        try:
            self._inner.record(task_text, outcome)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._push_learned_pref(outcome)
        except Exception as e:  # noqa: BLE001 -- learning never breaks a run
            log.info("context-pref push-back failed (%s), continuing", e)

    def _push_learned_pref(self, outcome: Dict[str, Any]) -> None:
        stashed = current_rep()
        if not stashed:
            return
        user_id = stashed.get("user_id")
        if not user_id:
            return
        sources = sources_consulted(outcome)
        if not sources:
            return               # nothing was really consulted, so there is nothing to learn
        pref = build_context_pref(sources)
        if not pref:
            return
        team_id = (stashed.get("task") or {}).get("team_id") or self._team_id
        if not team_id:
            return
        try:
            profile = self._client.get_ai_profile(user_id, team_id=team_id)
            if pref_is_duplicate(pref, (profile or {}).get("context_prefs")):
                return
        except Exception as e:  # noqa: BLE001 -- a failed read means skip, never push blind
            log.info("context_prefs dedupe read for %s failed (%s), skipping push", user_id, e)
            return
        try:
            self._client.add_context_pref(user_id, pref, team_id=team_id)
            log.info("pushed auto-learned context pref for rep %s: %r", user_id, pref)
        except Exception as e:  # noqa: BLE001
            log.info("context-pref push for %s failed (%s), continuing", user_id, e)
