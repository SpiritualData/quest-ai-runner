"""Secret-VALUE redaction for the file server: a thin binding over the hub's ``hubofhubs.act.secret_redaction``.

The patterns live in the hub; QAR adds only its own key shape (``qsk_``, a Quest API key).
"""
from __future__ import annotations

import re

from hubofhubs.api import secret_redaction as hub_redaction
from hubofhubs.api import (
    ASSIGNMENT_TEMPLATE, BARE_PLACEHOLDERS, CLI_LONG_FLAG, CLI_SHORT_P, DIGIT_REQUIRED_PATTERNS, KEY_ASSIGNMENT, PREFIXED_VALUE_PATTERNS, REDACTED,
    Redactor, STRONG_ASSIGNMENT, URI_CREDENTIALS, digit_gate, looks_like_reference, redact_assignment, redact_cli)  # noqa: F401  (the names importers already use)

QUEST_KEY_PATTERNS = (re.compile(r"(?<![A-Za-z0-9])qsk_[A-Za-z0-9]{16,}"),)


def redact(text: str) -> str:
    """Replace obvious secret values in ``text`` with ``[redacted]``, including Quest API keys. Never raises."""
    return hub_redaction.redact(text, QUEST_KEY_PATTERNS)
