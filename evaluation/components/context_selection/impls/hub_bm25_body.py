"""ALTERNATIVE: the hub's ``retrieve-context:bm25_body`` arm (``hubofhubs.retrieval.lexical.Bm25Body``) answering the context-selection cases, delegated, not copied
(see ``hub_seat.py``). No model call; the dense arms embed with the local bge-small model. Keeps no cards, so the store charge is zero."""
from hubofhubs.the_intelligence.subconscious.context_retriever.lexical import Bm25Body
from context_selection.hub_seat import build_arm

ALTERNATIVE = True
build = build_arm(Bm25Body, learned=False)
