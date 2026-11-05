"""ALTERNATIVE: the hub's ``retrieve-context:hybrid_rrf`` arm (``hubofhubs.retrieval.fusion.HybridRrf``) answering the context-selection cases, delegated, not copied
(see ``hub_seat.py``). No model call; the dense arms embed with the local bge-small model. Keeps no cards, so the store charge is zero."""
from hubofhubs.the_intelligence.subconscious.context_retriever.fusion import HybridRrf
from context_selection.hub_seat import build_arm

ALTERNATIVE = True
build = build_arm(HybridRrf, learned=False)
