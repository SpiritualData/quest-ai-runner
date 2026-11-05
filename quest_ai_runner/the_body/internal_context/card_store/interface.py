"""The interface of CardKeeping: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from esoi import solution
from hubofhubs.api import CardStore
from hubofhubs.api import CardRepository


@solution("card-store", kind="store", entry="search", example="rust release")
@typing.runtime_checkable
class CardKeeping(CardStore, typing.Protocol):
    """The hub's ``card-store`` interface (add, get, search, delete, count) over QAR's card persistence: a store the Quest card API or a
    local folder holds, which the arms below present as hub cards."""


@typing.runtime_checkable
class CardPersisting(CardRepository, typing.Protocol):
    """The cards a quest keeps, written and read back whole (the hub's card repository interface)."""
