"""What one turn used, for the closing line: the tokens and model calls of the whole turn (the shallow planner, answer and check calls too, not only a deep run) and how it is paid for.

The orchestrator already keeps the turn's tokens on its result (``tokens_in``, ``tokens_out``) and its provider counts calls per usage scope, so nothing is metered twice here. Dollars come only from
what a deep run reported; a lane whose model calls go through the operator's Claude subscription has no per-call price, and the closing says so (``cost_basis="subscription"``) instead of "cost unknown".
"""
from __future__ import annotations

import dataclasses
from typing import Any, Dict, Optional


def on_subscription(provider: Any) -> bool:
    """True when the primary model provider is the Claude Code CLI on the operator's login (the keyless lane): its calls have no price of their own."""
    primary = getattr(provider, "primary", provider)
    return type(primary).__name__ == "ClaudeCliProvider" or (hasattr(primary, "usage_limits") and hasattr(primary, "fresh_input_tokens"))


def turn_usage(final: Any, provider: Any, finished_type: type) -> Dict[str, Any]:
    """Keyword arguments for ``Finished``: ``tokens``, ``calls`` and ``cost_basis``, each only when known and only when this version of the hub's ``Finished`` carries the field."""
    tokens = int(getattr(final, "tokens_in", 0) or 0) + int(getattr(final, "tokens_out", 0) or 0) if final is not None else 0
    calls = int(getattr(provider, "call_count", 0) or 0)
    found: Dict[str, Any] = {"tokens": tokens or None, "calls": calls or None, "cost_basis": "subscription" if on_subscription(provider) else ""}
    known = {f.name for f in dataclasses.fields(finished_type)}
    return {key: value for key, value in found.items() if key in known and value}
