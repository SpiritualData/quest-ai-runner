"""The interface of CardBuilding: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from esoi import solution
from hubofhubs.api import CardsFromFiles


@solution("cards-from-files", kind="event", entry="build", example={"root": ".", "files": [], "existing": {}})
@typing.runtime_checkable
class CardBuilding(CardsFromFiles, typing.Protocol):
    """The hub's ``cards-from-files`` interface (``build`` and ``changed``) in QAR's scope, where QAR's records select the arm."""
