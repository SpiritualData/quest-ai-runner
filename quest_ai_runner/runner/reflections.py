"""Compatibility re-export: the reflections helpers now live in ``core.reflections``.

They depend only on the stdlib and a duck-typed client, and the ``QuestRetrievalAdapter`` needs
them, so the implementation moved inward (adapters must not import ``runner``). Old names stay.
"""
from __future__ import annotations

from ..core.reflections import *  # noqa: F401,F403
from ..core.reflections import (  # noqa: F401
    DEFAULT_PERIODS,
    MAX_FIELD_CHARS,
    ReflectionContext,
    _clip,
    _daily_payload,
    _period_payload,
    collect_reflections,
)
