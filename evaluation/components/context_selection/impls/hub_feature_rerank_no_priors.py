"""ALTERNATIVE: the hub's ``retrieve-context:feature_rerank_no_priors`` arm (``hubofhubs.retrieval.fusion.FeatureRerankNoPriors``) answering the context-selection cases, delegated, not copied
(see ``hub_seat.py``). No model call; the dense arms embed with the local bge-small model. Keeps no cards, so the store charge is zero."""
from hubofhubs.the_intelligence.subconscious.context_retriever.fusion import FeatureRerankNoPriors
from context_selection.hub_seat import build_arm

ALTERNATIVE = True
build = build_arm(FeatureRerankNoPriors, learned=False)
