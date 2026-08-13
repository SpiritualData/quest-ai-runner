"""sufficiency -- the STRUCTURAL half of the sufficiency gate.

``core/context_doctrine.SUFFICIENCY_GATE`` tells the planner, in prose, to issue another "read"
when it has not READ (not merely located) the material it is about to answer from. Prose alone was
the whole enforcement: nothing checked the plan the model returned, so a turn could surface a card
item that is only a stored SUMMARY of a larger source and answer straight from it, telling the user
"I only have the header, want me to go get it?" instead of going to get it. That is the failure this
module exists to make impossible.

The signal it keys off is STRUCTURAL, never the model's own words (CLAUDE.md hard rule #3): a
content item's ``locator`` declares, at capture time, that the text stored on the card is an
abridged stand-in for a larger source, and carries the READ SPEC that fetches the real thing::

    {"type": "note",
     "locator": {"text": "<the short synthesized summary>",
                 "abridged": true,                       # optional; implied by full_ref
                 "full_ref": {"query": {"kind": "goal_context", "goal_id": "...",
                                        "include_notes": true}}}}

``full_ref`` is an ordinary read spec, the same shape the planner emits in ``reads`` and the same
shape ``Orchestrator._exec_one_read`` executes, so the gate can force exactly the fetch the item
declares, with no knowledge of what kind of source it is. That keeps the brain domain-free: WHICH
source can be re-fetched, and how, is entirely the consumer's/adapter's statement about its own
data (hard rule #2).

Two things are then built on it:

  1. ``render_abridged_notice`` -- the notice woven into the context the planner sees, so the model
     can tell a summary from the full content it is normally handed (they used to render
     identically, which is why the model had no way to know). Same convention as
     ``orchestrator.truncate_verify_context``'s truncation marker: say plainly that what is shown
     is partial, and say what recovers the rest.
  2. ``unfetched_abridged`` -- the check the orchestrator runs before it lets a plan terminate in
     "answer": any declared-abridged item whose ``full_ref`` was NOT executed this turn forces one
     "read" step first. This honors the item's own declared inventory against a real signal (the
     read specs that actually ran), the same way ``core/guard.ExecutionRecord`` honors an answer's
     completion claims against what actually executed.

Nothing here fires unless a content item declares ``full_ref``: with no declaration the collected
list is empty, no notice is rendered, and the gate is inert, so an existing deployment's prompts
and behavior are byte-for-byte unchanged.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

# The keys that make a dict a READ SPEC the orchestrator can execute (``_exec_one_read``). A
# ``full_ref`` carrying none of them cannot be fetched, so it is dropped rather than forcing a
# read step that would do nothing.
READ_SPEC_KEYS = (
    "grep", "rel_path", "query",
    "list_sources", "describe_source", "list_operations", "describe_operation",
    "list_guidance", "read_guidance",
    "cards", "card",
)

ABRIDGED_NOTICE_HEADER = (
    "--- ABRIDGED CONTEXT ITEMS (stored SUMMARIES, not the full text) ---"
)


def is_read_spec(spec: Any) -> bool:
    """True when ``spec`` is a dict the orchestrator's read executor could actually run."""
    return isinstance(spec, dict) and any(spec.get(k) for k in READ_SPEC_KEYS)


def flatten_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a read spec's nested ``query`` params over its top level.

    The planner nests query params (``{"query": {"kind": ...}}``) while adapters read them at the
    top level, and ``Orchestrator._exec_one_read`` flattens the same way before dispatching. Doing
    it here too means "was this fetched?" compares the two shapes as equal instead of missing a
    match on a spelling difference. Never raises.
    """
    out: Dict[str, Any] = {}
    try:
        for k, v in (spec or {}).items():
            if k == "query" and isinstance(v, dict):
                out.update(v)
            else:
                out[k] = v
    except Exception:  # noqa: BLE001 -- a malformed spec just compares as itself
        return dict(spec) if isinstance(spec, dict) else {}
    return out


def spec_covers(executed: Dict[str, Any], wanted: Dict[str, Any]) -> bool:
    """True when the read spec ``executed`` already fetched everything ``wanted`` asks for.

    Coverage, not equality: the planner may issue the same fetch with extra filters, and that still
    pulled the content. Every key/value ``wanted`` names must be present and equal in ``executed``
    (both flattened first, so the nested and top-level query shapes compare the same). Never raises.
    """
    try:
        want = flatten_spec(wanted)
        have = flatten_spec(executed)
        if not want:
            return False
        for k, v in want.items():
            if k not in have or have[k] != v:
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


@dataclass
class AbridgedItem:
    """One context item whose stored text is a summary, plus the read spec that gets the full text.

    ``label`` is a short human/LLM-readable name for the item (its ``why``, the card it came from,
    or the item type), ``chars`` is how much summary text the card actually holds, and ``fetch`` is
    the item's declared ``full_ref`` read spec.
    """
    label: str
    fetch: Dict[str, Any]
    chars: int = 0
    item_id: str = ""
    card_id: str = ""
    item_type: str = "note"

    def describe(self) -> str:
        """One line for the planner: what is abridged, how short it is, and how to fetch it whole."""
        try:
            spec = json.dumps(self.fetch, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            spec = str(self.fetch)
        return (f"  - ({self.item_type}) {self.label}: the card holds {self.chars} characters of "
                f"SUMMARY, not the full text. Fetch the full text with this read spec: {spec}")


def collect_abridged_items(card_metadata: Optional[Iterable[Any]]) -> List[AbridgedItem]:
    """Extract every declared-abridged content item from an ``AssembledContext.card_metadata`` list.

    Walks ``card["items"]`` (the structured blocks ``card_content_render.render_card_content_blocks``
    produces, carried on card metadata by both retrieval arms) and keeps the items whose ``locator``
    declares a usable ``full_ref``. Deduped by fetch spec, so the same source surfacing on two cards
    forces one read, not two. Never raises: a store that carries no item blocks yields [].
    """
    items: List[AbridgedItem] = []
    seen: List[Dict[str, Any]] = []
    try:
        for card in (card_metadata or []):
            if not isinstance(card, dict):
                continue
            card_id = str(card.get("id") or "")
            for block in (card.get("items") or []):
                if not isinstance(block, dict):
                    continue
                locator = block.get("locator")
                if not isinstance(locator, dict):
                    continue
                fetch = locator.get("full_ref")
                if not is_read_spec(fetch):
                    continue
                if any(spec_covers(prev, fetch) and spec_covers(fetch, prev) for prev in seen):
                    continue
                seen.append(dict(fetch))
                label = (str(block.get("why") or "").strip()
                         or str(locator.get("label") or "").strip()
                         or card_id
                         or "context item")
                items.append(AbridgedItem(
                    label=label,
                    fetch=dict(fetch),
                    chars=len(str(locator.get("text") or "") or str(block.get("text") or "")),
                    item_id=str(block.get("id") or ""),
                    card_id=card_id,
                    item_type=str(block.get("type") or "note"),
                ))
    except Exception:  # noqa: BLE001 -- context assembly must never break a turn
        return items
    return items


def render_abridged_notice(items: Iterable[AbridgedItem]) -> str:
    """Render the planner-facing notice for ``items``. Returns "" when there is nothing to say.

    Deliberately says what to DO (issue the read spec) rather than only that something is missing:
    the failure this replaces was a reply that described its own insufficiency and asked the user
    for permission to resolve it.
    """
    lines = [it.describe() for it in (items or [])]
    if not lines:
        return ""
    return (ABRIDGED_NOTICE_HEADER + "\n"
            + "Some content above is a stored SUMMARY of a larger source, not the source itself.\n"
            + "Do NOT answer about its substance from the summary, and do NOT ask the user whether "
            "to go and get it: issue a \"read\" step with the fetch spec below, then answer from "
            "what comes back in GATHERED.\n"
            + "\n".join(lines))


def unfetched_abridged(items: Iterable[AbridgedItem],
                       executed_specs: Iterable[Dict[str, Any]]) -> List[AbridgedItem]:
    """Return the abridged items whose ``full_ref`` no read executed this turn has covered.

    ``executed_specs`` is the turn's real record of read specs that ran (see the orchestrator loop).
    This is the structural check: it compares the item's declared fetch against what ACTUALLY ran,
    never against anything the model said. Never raises.
    """
    pending: List[AbridgedItem] = []
    try:
        executed = [s for s in (executed_specs or []) if isinstance(s, dict)]
        for it in (items or []):
            if not any(spec_covers(done, it.fetch) for done in executed):
                pending.append(it)
    except Exception:  # noqa: BLE001
        return pending
    return pending


@dataclass
class AbridgedTurnState:
    """Per-turn bookkeeping for the gate: what is abridged, what was fetched, whether it fired.

    ``forced`` caps the gate at ONE forced read step per turn: the point is to close the specific
    "answered from a summary that was never opened" hole, not to give the loop a new way to spin.
    """
    items: List[AbridgedItem] = field(default_factory=list)
    executed: List[Dict[str, Any]] = field(default_factory=list)
    forced: bool = False

    def record_reads(self, specs: Optional[Iterable[Any]]) -> None:
        """Record the read specs a step actually executed. Never raises."""
        try:
            for spec in (specs or []):
                if isinstance(spec, dict):
                    self.executed.append(spec)
        except Exception:  # noqa: BLE001
            pass

    def pending(self) -> List[AbridgedItem]:
        return unfetched_abridged(self.items, self.executed)

    def should_force_read(self) -> List[AbridgedItem]:
        """The items to fetch before an "answer" may proceed ([] = let the answer through)."""
        if self.forced or not self.items:
            return []
        return self.pending()
