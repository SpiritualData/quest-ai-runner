"""ABLATION (offline arm), open gate: the same seeded cards and real ``FileContextStore.assemble`` as
``seeded_keyword_store``, but with ``confidence_threshold=0.0`` as the original standalone script set it
(it let the vector arm do the filtering). With no vector arm to filter, the open gate selects the best-scoring
card for ANY query, so it shows what the keyword arm alone does when it never abstains. No model, no network."""
from card_quality.impls import seeded_keyword_store


def build(meter):
    return seeded_keyword_store.build(meter, threshold=0.0)
