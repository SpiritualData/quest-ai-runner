"""Context cards as read-only views for the unified card and specification search (``core/unified_search.py``).

The one place a card repository meets the unified search: each stored card becomes a ``View`` whose search text is
``card_embed_text`` (the same text the vector arm embeds), so the two can never drift. Reads only; writes nothing.
"""
from __future__ import annotations

from typing import List

from esoi import spec_search as search_lib

from ..core.unified_search import CARD_SOURCE
from hubofhubs.recall.card_repository import CardRepository, card_embed_text


def card_views(repository: CardRepository, source: str = CARD_SOURCE) -> List[search_lib.View]:
    """A view per card the repository holds (``load_all``, which never raises); none for an empty or failing store."""
    return search_lib.views_from_items(repository.load_all() or {}, kind="card", source=source, text_of=card_embed_text)
