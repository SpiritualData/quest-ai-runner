"""The interface of RegistryTierSelection: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from esoi import solution
from hubofhubs.api import TierSelection


@solution("model-tier-chooser", kind="event", entry="choose", example={"available": ["anthropic"]})
@typing.runtime_checkable
class RegistryTierSelection(TierSelection, typing.Protocol):
    """QAR's tier chooser: the hub's ``model-tier-chooser`` interface with QAR's bars (``TIER_MIN_CASES`` and ``TIER_MIN_PASS_RATE``)
    as its defaults, so a measured pick has to clear them before it replaces the live model list."""
