"""ALTERNATIVE: the hub's ``retrieve-context:bm25_whole_files`` arm (``hubofhubs.retrieval.lexical.Bm25WholeFiles``) answering the context-selection cases, delegated, not copied
(see ``hub_seat.py``). No model call; the dense arms embed with the local bge-small model. Keeps no cards, so the store charge is zero."""
from hubofhubs.api import Bm25WholeFiles
from context_selection.hub_seat import build_arm

ALTERNATIVE = True
build = build_arm(Bm25WholeFiles, learned=False)
