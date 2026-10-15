"""The guidance port: ``GuidanceCard`` and ``GuidanceProvider``.

Split out of ``adapters.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# An OPTIONAL adapter role: GuidanceProvider (USE-CASE-SPECIFIC INSTRUCTIONS).
# ---------------------------------------------------------------------------
#
# Lets a host app keep its ALWAYS-ON core prompt small by moving instructions that
# apply to only SOME inputs (product facts, feature-flow guides, behavior policies) out of
# the static prompt and into a retrievable corpus of "guidance cards". The brain treats each
# card as OPAQUE TEXT — it knows nothing of what a card says, only that the host app supplies
# them. When a GuidanceProvider is wired, the orchestrator can pre-SELECT the cards most
# relevant to the user's message before planning, and the planner can also LIST the catalog
# and READ a specific card on demand, through the SAME observation machinery as a read.
#
# Purely additive: a consumer that supplies no GuidanceProvider sees exactly today's behavior.

@dataclass
class GuidanceCard:
    """One use-case-specific instruction unit, OPAQUE to the brain.

    Fields:
      ``id``        -- stable identifier the planner uses to ``read`` the card.
      ``title``     -- short human-facing name (shown in the catalog).
      ``relevance`` -- a one-line "when does this apply?" hint used for retrieval / pre-selection
                       (the catalog carries id + title + relevance, NOT the body, so it stays cheap).
      ``body``      -- the full instruction text. Empty in catalog (``list``) results; populated
                       by ``read`` / ``select``. The brain never interprets it — it just renders it.
      ``tags``      -- optional scope/operation tags used by UniversalGuidanceProvider for
                       hierarchical selection (e.g. "scope:global", "operation:plan").
    """
    id: str
    title: str
    relevance: str
    body: str = ""
    tags: List[str] = field(default_factory=list)


@runtime_checkable
class GuidanceProvider(Protocol):
    """OPTIONAL USE-CASE-SPECIFIC INSTRUCTIONS. The host app supplies retrievable guidance.

    All three methods NEVER raise — return ``[]`` / ``None`` on any error so a missing or
    degraded provider falls back gracefully (the run proceeds with no guidance).

    ``list``   — the CATALOG: every card's id + title + relevance, body EMPTY. Cheap; the brain
                 calls it (via ``list_guidance``) to discover what guidance exists.
    ``read``   — ONE card WITH body, by id; ``None`` if the id is unknown. The brain calls it
                 (via ``read_guidance``) when it needs the full instruction text.
    ``select`` — OPTIONAL semantic PRE-SELECTION: the top-``k`` cards (WITH bodies) most relevant
                 to ``user_message``. The orchestrator calls it ONCE before planning to inject an
                 "APPLICABLE GUIDANCE" block. May return ``[]`` (no opinion / not implemented).
    """

    def list(self) -> "List[GuidanceCard]":
        """The catalog (id + title + relevance; body empty). Cheap. Never raises."""

    def read(self, card_id: str) -> "Optional[GuidanceCard]":
        """One card WITH body by id; ``None`` if unknown. Never raises."""

    def select(
        self, user_message: str, *, k: int = 3, meta: Optional[Dict[str, Any]] = None
    ) -> "List[GuidanceCard]":
        """Top-``k`` cards (WITH bodies) most relevant to ``user_message``; may return [].
        ``meta`` carries the caller's scope (e.g. user/team/quest ids). Never raises."""
