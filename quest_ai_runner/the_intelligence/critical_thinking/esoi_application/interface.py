"""The interface of RecordsEditing: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

from typing import Protocol
from esoi import solution
from hubofhubs.api import EsoiApplying


@solution("esoi-application", kind="event", entry="apply", example={"change": {}})
class RecordsEditing(EsoiApplying, Protocol):
    """The hub's ``esoi-application`` interface: apply a proposed edit to the ESOI records, wherever they live."""
