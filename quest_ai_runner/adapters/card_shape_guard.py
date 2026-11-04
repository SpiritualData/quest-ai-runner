"""A card groups what several files say about one subject; a card per file is a file index, and none belongs in the card store.

This guard judges a batch of cards about to be written or uploaded by its shape: how many cards it holds per distinct file it names, and how
many of them pin a single file. A batch that names at least ``QAR_CARD_SHAPE_MIN_FILES`` files and goes over either limit is refused as a
whole, with a notice that gives the breakdown, never truncated to fit. The limits (``QAR_MAX_CARDS_PER_FILE``, ``QAR_MAX_SINGLE_FILE_CARD_SHARE``)
sit above what QAR's own topic bootstrap has written to its stores and far below one card per file, which is 1.0 cards per file and every card single.

It is a safety guard in the write path; which builder proposes the cards is chosen by the measured score of the ``cards-from-files`` arms.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping

from hubofhubs.recall.context_bootstrap_support import card_file_paths

from .card_store_settings import ENV_MAX_CARDS, card_shape_min_files, max_cards_per_file, max_single_file_card_share


@dataclass(frozen=True)
class Shape:
    cards: int      # cards that pin at least one file (a conversation or run card pins none and is not judged)
    files: int      # distinct files those cards name
    single: int     # cards that pin exactly one file

    @property
    def per_file(self) -> float:
        return self.cards / self.files if self.files else 0.0

    @property
    def single_share(self) -> float:
        return self.single / self.cards if self.cards else 0.0


def measure(cards: Iterable[Mapping[str, Any]]) -> Shape:
    """The shape of a batch of cards, counting only cards that pin files."""
    named, single, total = set(), 0, 0
    for card in cards:
        paths = card_file_paths(dict(card))
        if not paths:
            continue
        total += 1
        single += 1 if len(set(paths)) == 1 else 0
        named.update(paths)
    return Shape(cards=total, files=len(named), single=single)


def refusal(cards: Iterable[Mapping[str, Any]]) -> List[str]:
    """Two notice lines when the batch is a card per file by the limits in force, else an empty list."""
    shape = measure(cards)
    per_file_limit, single_limit = max_cards_per_file(), max_single_file_card_share()
    if shape.files < card_shape_min_files():
        return []
    over = []
    if per_file_limit and shape.per_file > per_file_limit:
        over.append(f"{shape.per_file:.2f} cards per file (at most {per_file_limit:g})")
    if single_limit and shape.single_share > single_limit:
        over.append(f"{shape.single_share:.0%} of the cards pin one file (at most {single_limit:.0%})")
    if not over:
        return []
    return [f"Refused {shape.cards} cards for {shape.files} files, {shape.single} pinning a single file: {'; '.join(over)}",
            "A card groups what several files say about one subject; a card per file is a file index. Build topic cards, "
            f"or change QAR_MAX_CARDS_PER_FILE and QAR_MAX_SINGLE_FILE_CARD_SHARE (0 = no rule); the card limit is {ENV_MAX_CARDS}"]
