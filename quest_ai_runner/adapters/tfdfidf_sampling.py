"""Compatibility re-export: the TF-DF-IDF sampling helpers now live in ``core.text_sampling``.

They are pure stdlib, and ``core`` modules need them, so the implementation moved inward (the
dependency rule: ``core`` imports nothing from ``adapters``). Every old name stays importable here.
"""
from __future__ import annotations

from ..core.text_sampling import *  # noqa: F401,F403
from ..core.text_sampling import (  # noqa: F401
    _NL_STOP,
    compute_idf,
    extract_terms,
    keywords_from_text,
    select_representatives,
)
