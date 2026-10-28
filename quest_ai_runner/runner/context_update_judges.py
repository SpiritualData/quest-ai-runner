"""The LLM judges that decide which updates are relevant and admitted.

Split out of ``context_updates.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional, Sequence

from .context_update_types import ContextUpdate, _clip, log
from ..core.prompt_specs import declare, render_prompt


# ---------------------------------------------------------------------------------------------
# Relevance: the engine's job, not the run's
# ---------------------------------------------------------------------------------------------

_RELEVANCE_PROMPT = """\
Someone is about to work on ONE piece of work. Below are things they captured recently. Captures
come from a personal quick-capture space and are about ALL parts of their life, so most of them
belong to some other piece of work.

THE WORK:
{work}

THE CAPTURES (each with the category tags the person chose for it):
{items}

Which of these could plausibly bear on THIS work? Answer with JSON and nothing else:
{{"relevant": [<numbers>]}}

How to decide:
  * Judge the SUBJECT MATTER of the work, not just its title. A one-line outcome names a
    destination, not a topic: read the whole description above to learn what this work is actually
    about, then ask whether the capture touches that.
  * A capture whose TAG names this work, or names the field this work is in, is strong evidence
    FOR it. The tags are the person's own labels for their own thinking, so a capture they filed
    under this work's subject almost always belongs here.
  * Material, contacts, observations and raw data in the work's subject area COUNT, even when the
    capture does not say what to do with them. Gathering is part of the work.
  * A capture about a different organization, a different job, or an unrelated part of their life
    does not belong here, even when it shares a word with the work.

WHEN IN DOUBT, INCLUDE IT. The costs are not symmetric: a capture shown that did not matter costs
one line in a brief, while a capture withheld that did matter is the person's own thinking silently
thrown away, and they will never know it happened. Exclude only what you are confident is about
something else.\
"""


def _describe_work(card: Dict[str, Any], label: str) -> str:
    """What this card is ABOUT, for a relevance judgment -- not just what it is called.

    An outcome is a destination ("I've completed my dissertation and have a PhD") and says nothing
    about the subject. Judged against that alone, a capture about the actual research topic reads
    as unrelated: verified live on 2026-09-10, when a capture the person had tagged for this very
    work was dropped from it. So everything the card knows about its own subject goes in.
    """
    parts = []
    if label:
        parts.append(f"Name: {label}")
    for key, heading in (("outcome", "Outcome"), ("description", "Description"),
                         ("current_state", "Current state"), ("instructions", "Standing brief")):
        value = _clip((card or {}).get(key), 600)
        if value:
            parts.append(f"{heading}: {value}")
    return "\n".join(parts) or label or "unnamed work"


def llm_relevance_judge(provider_fn: Callable[[], Any], tier: str = "balanced"
                        ) -> Callable[[str, str, Sequence[ContextUpdate]], Optional[set]]:
    """A judge that asks a model which user-scoped captures bear on THIS card.

    WHY THIS IS THE ENGINE'S JOB. Without it, every capture reaches every card and the RUN does the
    filtering, out loud, in the output a person reads: "Passed over: the Cornerstone capture
    (collaboration tracking) isn't this quest's domain." That line is the context engine's work
    showing up as the assistant's chatter. The person asked for a context engine, and an engine
    that hands over everything and lets the reader sort it out has not done the job.

    Deliberately a MODEL judgment and not a tag match against the card's name, which is what hard
    rule #3 in this repo's CLAUDE.md forbids and what ``runner/insights.py`` refuses to do: a fixed
    string rule silently drops every capture whose wording it did not anticipate ("dissertation" vs
    "thesis" vs no tag). Judging the subject is exactly the sanctioned alternative.

    BIASED TOWARD INCLUSION, and returns None on ANY failure, which the caller reads as "keep
    everything". A missing provider, an unroutable model, a timeout, or unparsable JSON must never
    be able to consume a person's own thinking on the way past: the worst case has to be a slightly
    noisier brief, never a silently emptier one.

    ``provider_fn`` is a callable rather than a provider because the engine is built before the CLI
    wraps ``cfg.model_provider`` with MultiProvider; resolving it at call time is what makes the
    judge use the routed provider rather than a raw one that 404s on half the model ids.
    """
    def _judge(work: str, _card_context: str,
               updates: Sequence[ContextUpdate]) -> Optional[set]:
        rows = list(updates or [])
        if not rows:
            return set()
        try:
            provider = provider_fn()
            if provider is None:
                return None
            from ..core.card_filter import _extract_json
            from ..core.model_registry import ModelRegistry
            try:
                model = ModelRegistry(provider).resolve_tier(tier or "balanced")
            except Exception:  # noqa: BLE001 -- an unresolvable registry must not stop the judge
                model = tier or "balanced"
            listed = "\n".join(
                f"{i}. [{', '.join(u.raw.get('categories') or []) or 'untagged'}] "
                f"{_clip(u.body or u.title, 300)}"
                for i, u in enumerate(rows, 1))
            # The work description goes in whole: ``_describe_work`` already clips each field,
            # and clipping the assembled text again cut it off before the description the judge
            # was given it for.
            prompt = render_prompt("feed-context-sources.relevance", work=(work or "").strip() or "unnamed work",
                                              items=listed)
            raw = provider.answer([{"role": "user", "content": prompt}], model=model)
            text = raw if isinstance(raw, str) else str(getattr(raw, "text", raw) or "")
            verdict = json.loads(_extract_json(text) or "{}")
            wanted = verdict.get("relevant")
            if not isinstance(wanted, list):
                return None
            keep = set()
            for n in wanted:
                try:
                    idx = int(n)
                except (TypeError, ValueError):
                    continue
                if 1 <= idx <= len(rows):
                    keep.add(rows[idx - 1].item_id or f"#{idx}")
            return keep
        except Exception as e:  # noqa: BLE001 -- see the docstring: failure means keep everything
            log.info("context updates: relevance judge unavailable (%s); keeping everything", e)
            return None

    return _judge


# ---------------------------------------------------------------------------------------------
# Admission: is anybody home? -- a different question from relevance, never conflated with it
# ---------------------------------------------------------------------------------------------

_ADMISSION_PROMPT = """\
A message reached a mailbox this assistant watches on somebody's behalf. It already survived a
deterministic header check for bulk/automated mail (List-Unsubscribe, Precedence: bulk,
Auto-Submitted and the like), so what remains is genuinely ambiguous: it might be a real person
writing in, or it might be marketing copy, a newsletter, or a notification that simply carries no
automation headers.

THE MESSAGES:
{items}

Which of these are a REAL PERSON writing to make a request, ask a question, or say something to a
human being -- as opposed to marketing, a newsletter, a product notification, or any other
machine-generated or mass-sent mail? Answer with JSON and nothing else:
{{"admit": [<numbers>]}}

How to decide:
  * A short personal note, a question, a reply, a request for something -- ADMIT it, even when it
    is terse or the sender is unfamiliar.
  * Marketing copy, a product announcement, a newsletter digest, a "see what's new" email, a
    donation-ask campaign -- these are NOT admitted, even when personally addressed and even when
    no header caught them.
  * A notification FROM a product or service ("your invoice is ready", "your order shipped") is
    not a person asking for anything and is not admitted.
  * When the wording alone cannot tell -- a short message with no clear signal either way -- ADMIT
    it. The costs are not symmetric: an admitted item that turns out to be nothing costs one line
    somebody dismisses in a moment; a real person's request dropped here is invisible, and nobody
    will ever know it happened, or that anyone was supposed to be reading this mailbox at all.\
"""


def llm_admission_judge(provider_fn: Callable[[], Any], tier: str = "fast"
                        ) -> Callable[[Sequence[ContextUpdate]], Optional[set]]:
    """A cheap judge asking a question relevance never asks: is this a real person, at all?

    RELEVANCE AND ADMISSION ARE DIFFERENT QUESTIONS, AND THIS REPO KEEPS THEM SEPARATE ON PURPOSE.
    ``llm_relevance_judge`` asks whether an item bears on THIS card's subject matter, once a card
    is already in the picture. This asks whether a message is a human being writing to us AT ALL,
    before any card enters the question. Conflating them would mean judging "is this spam" by
    whether it happens to mention this quest's outcome -- which a marketing email addressed to a
    real person's real interests can do by accident, and a genuine one-line request can fail to do
    by never restating the subject it is about. They are asked by different callers of the same
    shape (``UpdateEngine._apply_admission`` vs ``._apply_relevance``) against a class attribute
    each source declares independently (``judge_admission`` alongside ``judge_relevance`` on
    ``_BaseSource``), so a source can want one, the other, both, or neither.

    A CHEAP TIER ON PURPOSE (``"fast"`` here, not ``"balanced"``): the deterministic header check
    in ``adapters.inbound_mail.is_bulk_mail`` already removed whatever identified itself as bulk
    mail for free, so what reaches this judge is an already-narrowed remainder, and "is this a
    person" does not need the heavier tier a subject-matter judgment does.

    BIASED TOWARD INCLUSION, exactly like ``llm_relevance_judge`` and for the identical structural
    reason: returns None on ANY failure (no provider, an unroutable model, a timeout, unparsable
    JSON), which the caller reads as "keep everything". The asymmetry is total -- a real person's
    dropped request is invisible and nobody ever knows to ask again, while an admitted marketing
    email is one line somebody dismisses reading their asks -- so the failure mode has to favor the
    side that costs nothing.
    """
    def _judge(updates: Sequence[ContextUpdate]) -> Optional[set]:
        rows = list(updates or [])
        if not rows:
            return set()
        try:
            provider = provider_fn()
            if provider is None:
                return None
            from ..core.card_filter import _extract_json
            from ..core.model_registry import ModelRegistry
            try:
                model = ModelRegistry(provider).resolve_tier(tier or "fast")
            except Exception:  # noqa: BLE001 -- an unresolvable registry must not stop the judge
                model = tier or "fast"
            listed = "\n".join(
                f"{i}. from {u.author or 'an unknown sender'}: {_clip(u.title, 120)}\n"
                f"   {_clip(u.body or u.excerpt, 300)}"
                for i, u in enumerate(rows, 1))
            prompt = render_prompt("feed-context-sources.admission", items=listed)
            raw = provider.answer([{"role": "user", "content": prompt}], model=model)
            text = raw if isinstance(raw, str) else str(getattr(raw, "text", raw) or "")
            verdict = json.loads(_extract_json(text) or "{}")
            wanted = verdict.get("admit")
            if not isinstance(wanted, list):
                return None
            keep = set()
            for n in wanted:
                try:
                    idx = int(n)
                except (TypeError, ValueError):
                    continue
                if 1 <= idx <= len(rows):
                    keep.add(rows[idx - 1].item_id or f"#{idx}")
            return keep
        except Exception as e:  # noqa: BLE001 -- see the docstring: failure means keep everything
            log.info("context updates: admission judge unavailable (%s); keeping everything", e)
            return None

    return _judge


RELEVANCE_SPEC = declare(
    "feed-context-sources.relevance", solution="feed-context-sources",
    metric="metric-feed-context-sources", tier="balanced", cache_layer="none",
    template=_RELEVANCE_PROMPT)
ADMISSION_SPEC = declare(
    "feed-context-sources.admission", solution="feed-context-sources",
    metric="metric-feed-context-sources", tier="fast", cache_layer="none",
    template=_ADMISSION_PROMPT)
