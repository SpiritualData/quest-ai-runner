"""QAR's wiring of the hub's keyless ``claude_cli`` arm (``hubofhubs.models.claude_cli``): the provider that drives ``claude -p`` on the
operator's subscription login. The arm itself is the hub's; this module only binds what is QAR's: the lane-wide subscription usage
limit (``core.usage_limit``), the live newest-model lookup (``claude_live_models``) and the ``QAR_CLI_EFFORT`` and
``QAR_CLI_HAIKU_THINKING_TOKENS`` settings, read when a call is made.

``cli_model`` is the hub's with the live lookup bound; importers of the other names (``extract_json_object``, ``CLI_RUNNABLE_MODELS``,
the flatten helpers) use ``hubofhubs.models.claude_cli``.
"""
from __future__ import annotations

import os
from typing import Optional

from esoi import implementation
from hubofhubs.models import claude_cli as hub

from ..core import usage_limit
from .claude_live_models import newest_claude_id


def cli_model(model: Optional[str]) -> Optional[str]:
    """The CLI's ``--model`` argument for a resolved model id (the hub's mapping, with the newest live id of a family)."""
    return hub.cli_model(model, newest_claude_id)


@implementation("call-a-model", "claude_cli")
class ClaudeCliProvider(hub.ClaudeCliProvider):
    """The hub's ``claude_cli`` arm with QAR's usage-limit state, live model lookup and environment settings bound."""

    def __init__(self, **settings):
        settings.setdefault("usage_limits", usage_limit)
        settings.setdefault("newest_family_id", newest_claude_id)
        settings.setdefault("effort", lambda: os.getenv("QAR_CLI_EFFORT"))
        settings.setdefault("haiku_thinking_tokens", lambda: os.getenv("QAR_CLI_HAIKU_THINKING_TOKENS"))
        settings.setdefault("environment", lambda: os.environ)
        super().__init__(**settings)
