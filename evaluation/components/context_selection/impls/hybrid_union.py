"""ALTERNATIVE: keyword + semantic fusion, as ``HybridContextAssembler`` does. The candidate set is the UNION
of the top 3 of ``idf_docstrings`` and the top 3 of ``vector_bge_small`` (keyword catches exact identifiers,
vector catches paraphrase), ordered by reciprocal-rank fusion of the two rankings. It may return up to 6
files, which it declares with ``select.budget = 6`` so the scorer reports both the strict top-3 of the
fused order and recall within the 6 the union is allowed to spend."""
from context_selection.impls import idf_docstrings, vector_bge_small

RRF_K = 60


def build(meter):
    keyword, vector = idf_docstrings.build(meter), vector_bge_small.build(meter)

    def select(task, corpus):
        a, b = keyword(task, corpus), vector(task, corpus)
        fused = {}
        for ranking in (a, b):
            for rank, rel in enumerate(ranking):
                fused[rel] = fused.get(rel, 0.0) + 1.0 / (RRF_K + rank + 1)
        union = list(dict.fromkeys(a[:3] + b[:3]))
        return sorted(union, key=lambda rel: (-fused[rel], rel))
    select.budget = 6
    return select
