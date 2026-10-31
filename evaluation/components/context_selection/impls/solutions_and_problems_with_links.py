"""CANDIDATE: the unified card and specification search (``core/unified_search.solutions_and_problems_with_links``) on this
component's own tasks, the context_selection half of metric-search-cards-and-specs. Records are absent: the views are the
corpus cards (``esoi.spec_search.views_from_items`` with ``card_embed_text``, the text the production vector arm embeds), the vector search
is cosine over the same local bge-small embedder the free arms use, keyed by view key, and no provider is passed, so the arm
makes zero model calls. The implementation keeps its own settings (at most five ranked items); each card's files are returned
in its ranked order and the scorer reads the first three, as for every arm here. Every card is embedded by the model
itself (``uncached_embedder``), so the run never writes the shared text-hash cache; it takes about a minute and a half."""
from context_selection.embedder import uncached_embedder
from hubofhubs.recall.card_repository import card_embed_text
from esoi import spec_search
from quest_ai_runner.core.unified_search import solutions_and_problems_with_links


def build(meter):  # noqa: ARG001
    import numpy as np
    embed = uncached_embedder()
    built = {}

    def world_for(corpus):
        key = str(corpus.root)
        if key not in built:
            views = spec_search.views_from_items(corpus.cards, kind="card", source="cards", text_of=card_embed_text)
            keys = [v.key for v in views]
            matrix = embed([card_embed_text(corpus.cards[v.id]) or corpus.cards[v.id]["files"][0]["path"] for v in views])
            matrix = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9)

            def vector_search(text, k):
                q = embed([text])[0]
                scores = matrix @ (q / (np.linalg.norm(q) + 1e-9))
                return [(keys[i], float(scores[i])) for i in np.argsort(-scores, kind="stable")[:k]]
            built[key] = (views, vector_search)
        return built[key]

    def select(task, corpus):
        views, vector_search = world_for(corpus)
        got = solutions_and_problems_with_links(task, views, vector_search=vector_search)
        paths = []
        for item in got.items:
            for f in corpus.cards.get(item.id, {}).get("files", []):
                if f["path"] not in paths:
                    paths.append(f["path"])
        return paths
    return select
