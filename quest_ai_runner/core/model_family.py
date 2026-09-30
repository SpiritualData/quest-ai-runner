"""Resolve a bare model FAMILY name to the newest live model of that family, for any vendor.

``sonnet``, ``claude-opus``, ``gemini-flash``, ``gemini-flash-lite``, ``gpt`` or ``gpt-mini`` name a
family, not a release. Config that names only the family never needs editing when a new release
ships: the newest live id of that family is used. Anything containing a digit (``gpt-4o``,
``claude-sonnet-4-6``, ``gemini-2.5-flash``) is a pinned release and passes through untouched.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

VENDOR_PREFIXES = ("claude", "gemini", "gpt")
# Suffixes that mark a build of the same family rather than a different family.
NOISE_TOKENS = {"preview", "latest", "exp", "experimental"}


def split_tokens(model_id: str) -> Tuple[List[str], Tuple[int, ...]]:
    """(family words, version numbers) of an id. Single letters ("o" in gpt-4o) are not family words."""
    low = (model_id or "").strip().lower()
    if low.startswith("models/"):
        low = low[len("models/"):]
    parts = re.findall(r"\d+|[a-z]+", low)
    words = [p for p in parts if p.isalpha() and len(p) > 1 and p not in NOISE_TOKENS]
    nums = tuple(int(p) for p in parts if p.isdigit())
    return words, nums


def is_family_name(name: Optional[str]) -> bool:
    low = (name or "").strip().lower()
    return bool(low) and not re.search(r"\d", low)


def newest_in_family(name: Optional[str], live_models: Iterable[str]) -> Optional[str]:
    """The newest live id whose family equals ``name``, or None (not a family name / no match)."""
    if not is_family_name(name):
        return None
    want, _ = split_tokens(name)
    if not want:
        return None
    best: Optional[Tuple[Tuple[int, ...], str]] = None
    for mid in live_models:
        words, nums = split_tokens(mid)
        if not words:
            continue
        shapes = [words]
        if words[0] in VENDOR_PREFIXES:
            shapes.append(words[1:])  # "sonnet" also names claude-sonnet-*
        if want in shapes and (best is None or nums > best[0]):
            best = (nums, mid)
    return best[1] if best else None


def resolve_model_name(model: str, live_models: Iterable[str]) -> str:
    """``model`` itself unless it is a family name with a live match."""
    return newest_in_family(model, live_models) or model
