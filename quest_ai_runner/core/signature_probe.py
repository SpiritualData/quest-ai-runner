"""Version-skew primitives: which keywords an installed class or function accepts, and a filter.

A consumer and QAR can ship as separate deploys with no order guarantee, so a consumer that passes a
brand-new keyword to a stale library would raise ``TypeError`` on its hot path. The consumer decides
support by INSPECTING the installed signature (never by comparing version strings) and drops what the
library does not understand. These are the generic primitives; which keywords matter is the
consumer's own table.
"""
from __future__ import annotations

import dataclasses
import inspect
import logging
from typing import Any, Dict, Optional, Set

log = logging.getLogger(__name__)


def accepted_keywords(fn: Any) -> Optional[Set[str]]:
    """Keyword names ``fn`` accepts, or None when it takes arbitrary ``**kwargs`` or has no signature.

    None means "cannot rule anything out": every filter treats it as full support, because a callable
    that takes ``**kwargs`` will handle (or explicitly reject) a keyword itself.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # builtins / C-level callables carry no signature
        return None
    names: Set[str] = set()
    for param in sig.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return None
        if param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            names.add(param.name)
    names.discard("self")
    return names


def accepted_fields(cls: Any) -> Optional[Set[str]]:
    """Field names ``cls`` accepts at construction (dataclass init fields, else ``__init__`` keywords)."""
    try:
        if dataclasses.is_dataclass(cls):
            return {f.name for f in dataclasses.fields(cls) if f.init}
    except Exception:  # noqa: BLE001 - a probe must never raise
        pass
    try:
        return accepted_keywords(cls.__init__)
    except Exception:  # noqa: BLE001
        return None


def filter_keywords(kwargs: Dict[str, Any], allowed: Optional[Set[str]], what: str) -> Dict[str, Any]:
    """``kwargs`` minus any key ``allowed`` does not contain (None allows everything); drops are logged."""
    if allowed is None:
        return dict(kwargs)
    kept = {key: value for key, value in kwargs.items() if key in allowed}
    dropped = sorted(set(kwargs) - set(kept))
    if dropped:
        log.debug("%s: dropped unsupported keywords %s", what, dropped)
    return kept
