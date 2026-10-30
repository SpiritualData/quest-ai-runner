"""A card repository that behaves like the Quest backend, in memory: the tests and the repository_tidy arm use it.

What makes it backend-shaped: there is no quarantine folder (``delete`` is final), every write gets a new
server revision and ``updated_at`` (fields a writer never sets), ``revision()`` changes on every write, and a
write refuses an id no writer may store (``card_origin.refuse``), as every repository does. ``seed`` puts cards
in the way an older version left them, ids that writers now refuse included. ``fail`` makes a chosen call fail
(returning False, as the port promises), and ``calls`` records every write and delete in order.
"""
from __future__ import annotations

import copy
import itertools
from typing import Any, Callable, Dict, List, Optional, Tuple

from hubofhubs.recall import card_origin

SERVER_FIELDS = ("updated_at", "revision", "owner_user_id", "user_id", "card_id")


class BackendShapedRepository:
    base_url = "memory://card-backend"

    def __init__(self) -> None:
        self.cards: Dict[str, Dict[str, Any]] = {}
        self.counter = itertools.count(1)
        self.stamp = 0
        self.calls: List[Tuple[str, str]] = []
        self.fail: Optional[Callable[[str, str], bool]] = None
        self.before_change: Optional[Callable[[str, str], None]] = None

    def stored(self, card: Dict[str, Any]) -> Dict[str, Any]:
        out = {k: copy.deepcopy(v) for k, v in card.items() if k not in SERVER_FIELDS}
        n = next(self.counter)
        out["revision"] = n
        out["updated_at"] = float(n)
        self.stamp += 1
        return out

    def seed(self, key: str, card: Dict[str, Any]) -> None:
        self.cards[key] = self.stored(card)

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        return {k: copy.deepcopy(v) for k, v in self.cards.items()}

    def read(self, key: str) -> Optional[Dict[str, Any]]:
        card = self.cards.get(key)
        return copy.deepcopy(card) if card is not None else None

    def exists(self, key: str) -> bool:
        return key in self.cards

    def revision(self) -> Any:
        return self.stamp

    def write(self, key: str, card: Dict[str, Any]) -> bool:
        if self.before_change is not None:
            self.before_change("write", key)
        if card_origin.refuse(key) or (self.fail is not None and self.fail("write", key)):
            return False
        self.calls.append(("write", key))
        self.cards[key] = self.stored(card)
        return True

    def delete(self, key: str) -> bool:
        if self.before_change is not None:
            self.before_change("delete", key)
        if self.fail is not None and self.fail("delete", key):
            return False
        self.calls.append(("delete", key))
        self.cards.pop(key, None)
        self.stamp += 1
        return True
