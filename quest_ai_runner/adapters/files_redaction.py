"""Secret-VALUE redaction for the file server: a thin binding over the hub's ``hubofhubs.act.secret_redaction``.

The patterns live in the hub; QAR adds only its own key shape (``qsk_``, a Quest API key).
"""
from __future__ import annotations

import re

from hubofhubs.act import secret_redaction as hub_redaction
from hubofhubs.act.secret_redaction import *  # noqa: F401,F403 - the names importers already use

QUEST_KEY_PATTERNS = (re.compile(r"(?<![A-Za-z0-9])qsk_[A-Za-z0-9]{16,}"),)


def redact(text: str) -> str:
    """Replace obvious secret values in ``text`` with ``[redacted]``, including Quest API keys. Never raises."""
    return hub_redaction.redact(text, QUEST_KEY_PATTERNS)
