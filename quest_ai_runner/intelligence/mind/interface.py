"""The interfaces of StepReasoning: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from esoi import solution
from hubofhubs.api import Reasoning


@solution("the-mind", kind="event", entry="think", example={"request": "What should I work on first this week?"})
@typing.runtime_checkable
class StepReasoning(Reasoning, typing.Protocol):
    """The hub's ``the-mind`` interface: one request thought through, and the part of a step of the loop."""
