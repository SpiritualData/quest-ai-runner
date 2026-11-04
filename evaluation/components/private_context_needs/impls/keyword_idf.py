"""BASELINE (``private-context-needs:keyword_idf``): rank the candidates by TF-IDF cosine with the task and select the best few whose
score reaches a share of the best one. No model, no embedder. Ranks single items, so a topic spread over several sources is found only
as far as each piece shares words with the task."""
from private_context_needs.text_terms import cosine, idf_weights, tokens, vector

TOP_K, MIN_SHARE, MIN_SCORE = 5, 0.5, 0.05


def build(meter):  # noqa: ARG001
    def select(task, candidates):
        docs = [tokens(c["text"]) for c in candidates]
        idf = idf_weights(docs + [tokens(task)])
        q = vector(tokens(task), idf)
        scored = sorted(((cosine(q, vector(d, idf)), c["item_id"]) for d, c in zip(docs, candidates)), reverse=True)
        if not scored or scored[0][0] < MIN_SCORE:
            return []
        return [i for s, i in scored[:TOP_K] if s >= scored[0][0] * MIN_SHARE]
    return select
