"""ALTERNATIVE: the hub's ``retrieve-context:bm25_fielded`` arm (``hubofhubs.retrieval.lexical.Bm25Fielded``) answering the context-selection cases, delegated, not copied
(see ``hub_seat.py``). No model call; the dense arms embed with the local bge-small model. Keeps no cards, so the store charge is zero."""
from hubofhubs.retrieval.lexical import Bm25Fielded
from context_selection.hub_seat import build_arm

ALTERNATIVE = True
build = build_arm(Bm25Fielded, learned=False)
