"""The fixture world every arm searches: generic cards, the spec_selection fixture index with a few extra records (a
problem no solution serves, an intent no solution serves), a fixture tag table, and one local vector search over all of it.

Nothing here is real data. The index is ``spec_selection/fixture_index.json`` (the output of ``esoi spec index``); the
extra records are folded in exactly as the index folds a record (``unified_search.weighted_terms``), with the document
frequencies recomputed, so both pipelines see the same records. The vector search is the local bge-small embedder the
context-selection and spec-selection offline arms already use (``context_selection.embedder``, read offline from the
fastembed cache, raising when the model is missing): no provider and no network.
"""
import copy
import json
from collections import Counter
from pathlib import Path

import common  # noqa: F401  (sets sys.path for the repo)
from quest_ai_runner.adapters.card_repository import card_embed_text
from quest_ai_runner.core.spec_selection import card_line
from quest_ai_runner.core.unified_search import TagTable, views_from_cards, views_from_spec_index, weighted_terms

HERE = Path(__file__).resolve().parent
FIXTURES = json.loads((HERE / "fixtures.json").read_text(encoding="utf-8"))
BASE_INDEX = json.loads((HERE.parent / "spec_selection" / "fixture_index.json").read_text(encoding="utf-8"))
CARD_PREFIX = "card:"


def spec_index():
    """The fixture index plus the extra records, document frequencies recomputed."""
    index = copy.deepcopy(BASE_INDEX)
    for extra in FIXTURES["extra_records"]:
        entry = dict(extra, terms=weighted_terms(extra["title"], extra["summary"]))
        index["entries"].append(entry)
    df = Counter()
    for entry in index["entries"]:
        df.update(entry["terms"].keys())
    index["df"], index["n"] = dict(df), len(index["entries"])
    return index


def cards():
    return {cid: dict(card, id=cid) for cid, card in FIXTURES["cards"].items()}


def embedder():
    """``embed(texts) -> array`` with the local bge-small model the offline arms use (``context_selection.embedder.MODEL``),
    read offline from the fastembed cache; it raises when the model is not cached, never degrades to no vector search. No
    on-disk vector cache: the fixture world is small enough to embed in seconds."""
    import os

    import numpy as np
    from context_selection.embedder import MODEL
    from fastembed import TextEmbedding
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    model = TextEmbedding(model_name=MODEL, cache_dir=os.environ.get("FASTEMBED_CACHE_PATH", "/tmp/fastembed_cache"))

    def embed(texts):
        return np.array([[float(x) for x in v] for v in model.embed([t[:2000] for t in texts])])
    return embed


def tag_table():
    table = FIXTURES["tag_table"]
    return TagTable(tag_problems={k: tuple(v) for k, v in table["tag_problems"].items()},
                    solution_tags={k: tuple(v) for k, v in table["solution_tags"].items()})


class World:
    """Built once per run: the index, the cards, their views, and a vector search keyed by view key (a record's id,
    ``card:<id>`` for a card)."""

    def __init__(self, vector=True):
        self.index = spec_index()
        self.cards = cards()
        self.tags = tag_table()
        self.card_views = views_from_cards(self.cards, text_of=card_embed_text)
        self.record_views = views_from_spec_index(self.index, self.tags)
        self.views = self.card_views + self.record_views
        self.vector_search = self.make_vector_search() if vector else None

    def make_vector_search(self):
        import numpy as np
        embed = self.embed = embedder()
        texts = {v.key: card_embed_text(self.cards[v.id]) for v in self.card_views}
        texts.update({e["id"]: card_line(e) for e in self.index["entries"]})
        keys = list(texts)
        matrix = embed([texts[k] for k in keys])
        matrix = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9)

        def search(text, k, only=None):
            q = embed([text])[0]
            scores = matrix @ (q / (np.linalg.norm(q) + 1e-9))
            ranked = [(keys[i], float(scores[i])) for i in np.argsort(-scores, kind="stable")]
            if only is not None:
                ranked = [(key, s) for key, s in ranked if only(key)]
            return ranked[:k]
        return search

    def records_search(self, text, k):
        """The vector search over the records alone, as ``find_specs`` sees them."""
        return self.vector_search(text, k, only=lambda key: not key.startswith(CARD_PREFIX))


WORLDS = {}


def world(vector=True):
    """One world per process and setting: the cases share it."""
    if vector not in WORLDS:
        WORLDS[vector] = World(vector)
    return WORLDS[vector]
