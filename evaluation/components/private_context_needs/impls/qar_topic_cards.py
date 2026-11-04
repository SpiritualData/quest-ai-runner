"""ARM (``private-context-needs:qar_topic_cards``): QAR's own selector over CARDS. The candidates are first grouped into topics (items
whose TF-IDF vectors are close join one card, whatever their source or type), each topic becomes one context card (keywords and summary
from its members), the cards are written to a throwaway folder and the real ``FileContextStore.assemble`` selects cards for the task.
The members of every selected card are the context. No model, no embedder; the grouping is deterministic. This is the shape the card
solution is meant to have (one card per topic, drawn from many sources) with a rule where production uses a model to write the card."""
import json
import tempfile
from pathlib import Path

from private_context_needs.text_terms import cosine, idf_weights, tokens, vector

JOIN_COSINE = 0.35
KEYWORDS_PER_CARD = 14


def group_topics(candidates):
    """Single-link clusters of candidates whose TF-IDF cosine reaches JOIN_COSINE. Returns a list of index lists."""
    docs = [tokens(c["text"]) for c in candidates]
    idf = idf_weights(docs)
    vecs = [vector(d, idf) for d in docs]
    parent = list(range(len(docs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(docs)):
        for j in range(i + 1, len(docs)):
            if cosine(vecs[i], vecs[j]) >= JOIN_COSINE:
                parent[find(i)] = find(j)
    groups = {}
    for i in range(len(docs)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values()), docs, idf


def card_for(index, members, candidates, docs, idf):
    weight = {}
    for m in members:
        for t in set(docs[m]):
            weight[t] = weight.get(t, 0.0) + idf.get(t, 1.0)
    keywords = [t for t, _ in sorted(weight.items(), key=lambda kv: (-kv[1], kv[0]))[:KEYWORDS_PER_CARD]]
    summary = " ".join((candidates[m]["text"] or "")[:160] for m in members[:2])
    return {"id": f"topic-{index}", "keywords": keywords, "summary": summary, "files": [], "content": [], "conventions": [],
            "provenance": {"created_by_task": "", "model": "", "created_at": "", "last_verified_at": ""},
            "usage_count": 0, "last_outcome": "unknown"}


def build(meter):  # noqa: ARG001
    from quest_ai_runner.adapters.file_context_store import FileContextStore

    def select(task, candidates):
        if not candidates:
            return []
        groups, docs, idf = group_topics(candidates)
        with tempfile.TemporaryDirectory(prefix="qar_private_cards_") as folder:
            for k, members in enumerate(groups):
                card = card_for(k, members, candidates, docs, idf)
                (Path(folder) / f"{card['id']}.json").write_text(json.dumps(card), encoding="utf-8")
            store = FileContextStore(folder, auto_bootstrap=False)
            assembled = store.assemble(task, read_only=True)
        wanted = set(assembled.card_ids or [])
        return [candidates[m]["item_id"] for k, members in enumerate(groups) if f"topic-{k}" in wanted for m in members]
    return select
