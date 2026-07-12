"""PromptLayers -- the ONE shared prompt-assembly primitive (cache-friendly layering).

Provider prompt caches are PREFIX caches: a call reads from cache only the LONGEST byte-identical
prefix it has already seen. A single turn makes many LLM calls (plan, re-plan, answer, verify,
overseer); when each builds a differently-shaped prompt they share no prefix, so every call pays
full input price (measured: 0 cached tokens). Layering fixes that by making every call start from
the SAME [head][context] prefix and vary only in a per-call tail:

  L1 head    -- stable system / persona / standards text, identical for the whole conversation.
  L2 context -- the assembled context / cards block, stable-ordered upstream (by card id, never by
                per-turn relevance score); identical while the card selection is unchanged.
  L3 tail    -- the volatile per-call part: date / time, the step instruction (plan vs re-plan vs
                answer vs verify), the new message, the gathered observations. NEVER cached.

Because L1 and L2 render byte-identical across the calls of a turn (and across turns while the
cards are unchanged), a lineage re-reads that prefix from cache instead of re-sending it, and only
the tail is fresh. The same rendered layers can feed several cached lineages (the cheap worker and
the best-tier overseer); per-model caches never cross, so each keeps its own copy.

Generic (hard rule #2): nothing here knows about any org, model, or provider. A ``ModelProvider``
maps the ordered blocks onto its own cache mechanism (Anthropic ``cache_control`` breakpoints,
Gemini ``system_instruction`` + stable ``contents``). A provider that supports no caching just
joins the block texts back into the plain-string shape, so behavior is unchanged when nobody reads
the cache markers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

# A single ordered prompt block handed to a provider: its text and whether that text is a stable,
# cache-eligible layer. ``{"text": str, "cache": bool}``.
LayerBlock = Dict[str, Any]

# Providers that expose explicit cache breakpoints cap how many a single request may carry
# (Anthropic allows at most four ``cache_control`` markers). The layering never needs more than a
# couple (head + context), but this is the shared ceiling a provider guards against.
MAX_CACHE_BREAKPOINTS = 4

# The separator between layers when they are flattened back into one plain string. Kept as a blank
# line so the flattened form reads naturally and ``prefix()`` stays a true byte-prefix of
# ``render()`` (see ``join_layers``).
LAYER_SEPARATOR = "\n\n"


def join_layers(*parts: str) -> str:
    """Join non-empty layer strings in order with the shared separator.

    Empty parts are dropped so a missing layer never leaves a doubled separator. Because parts are
    joined in the given order and empties are dropped consistently, the join of a leading subset is
    always a byte-prefix of the join of the full sequence (relied on by ``PromptLayers.prefix``).
    """
    return LAYER_SEPARATOR.join(p for p in parts if p)


def blocks_to_prompt(blocks: List[LayerBlock]) -> str:
    """Flatten ordered layer blocks back into ONE plain string (the graceful-degradation shape).

    A provider with no cache support calls this to recover exactly the prompt it would have built
    from the flattened layers, so passing ``layers`` never changes behavior for such a provider.
    """
    return join_layers(*[str(b.get("text") or "") for b in (blocks or [])])


@dataclass
class PromptLayers:
    """One turn-call's prompt, split into the three cache layers.

    ``head`` (L1) and ``context`` (L2) are the cache-eligible prefix; ``tail`` (L3) is volatile.
    Build every plan / re-plan / answer / verify call through this so their L1 + L2 render
    byte-identical and only the tail differs.
    """

    head: str = ""      # L1 -- stable head (system / persona / standards)
    context: str = ""   # L2 -- context / cards layer (stable-ordered)
    tail: str = ""      # L3 -- volatile tail (step instruction + new message + gathered + date)

    def prefix(self) -> str:
        """The cache-eligible portion (L1 + L2) as one string.

        This is the byte-identical prefix that sibling calls share; it is always a byte-prefix of
        ``render()``.
        """
        return join_layers(self.head, self.context)

    def render(self) -> str:
        """Flatten to ONE plain string (head + context + tail) for a plain-string call path."""
        return join_layers(self.head, self.context, self.tail)

    def blocks(self) -> List[LayerBlock]:
        """The ordered layer blocks: head and context marked ``cache=True``, tail ``cache=False``.

        Empty head / context layers are dropped so a provider never emits an empty cached block.
        The tail block is always present (even when empty) so there is always a final, volatile
        turn for the provider to attach. The number of ``cache=True`` blocks never exceeds
        ``MAX_CACHE_BREAKPOINTS``.
        """
        out: List[LayerBlock] = []
        if self.head.strip():
            out.append({"text": self.head, "cache": True})
        if self.context.strip():
            out.append({"text": self.context, "cache": True})
        out.append({"text": self.tail, "cache": False})
        return out


def turn_prompt_head(persona: str = "", standards: str = "") -> str:
    """Render the stable L1 head from a turn's persona and quality standards.

    Deterministic in its inputs: the SAME (persona, standards) always yield the SAME bytes, which is
    what lets plan / answer / verify share a byte-identical head when they are built from the same
    turn constants. Either part may be empty; an empty head is fine (the block is simply dropped).
    """
    parts: List[str] = []
    p = (persona or "").strip()
    if p:
        parts.append("--- ACT AS THIS PERSONA ---\n" + p)
    s = (standards or "").strip()
    if s:
        parts.append("--- QUALITY STANDARDS (the bar the result must meet) ---\n" + s)
    return "\n\n".join(parts)


def compose_layers(*, persona: str = "", standards: str = "", context: str = "",
                   tail: str = "") -> PromptLayers:
    """THE shared prompt-assembly entry point every turn-call routes through.

    Builds the stable L1 head from ``persona`` + ``standards`` (deterministic in those inputs), sets
    L2 to the ``context`` block, and takes the per-call ``tail`` as L3. Because the head and context
    depend only on turn constants, plan / re-plan / answer / verify calls built from the same
    (persona, standards, context) render a byte-identical ``prefix()`` and differ only in the tail.
    """
    return PromptLayers(head=turn_prompt_head(persona, standards), context=context or "", tail=tail or "")


def cache_control_indices(blocks: List[LayerBlock], *, max_breakpoints: int = MAX_CACHE_BREAKPOINTS) -> List[int]:
    """The indices of ``blocks`` that should carry a cache breakpoint, capped at ``max_breakpoints``.

    Every ``cache=True`` block is a candidate. If more candidates exist than the provider allows,
    keep the LAST ``max_breakpoints`` of them: a breakpoint caches everything up to and including
    its block, so keeping the later markers still caches the longest shared prefixes. Shared by any
    provider that renders explicit cache breakpoints, so the cap is enforced in ONE place.
    """
    candidates = [i for i, b in enumerate(blocks or []) if b.get("cache")]
    if len(candidates) > max_breakpoints:
        candidates = candidates[-max_breakpoints:]
    return candidates
