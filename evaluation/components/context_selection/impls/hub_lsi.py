"""ALTERNATIVE: the hub's ``retrieve-context:lsi`` arm (``hubofhubs.retrieval.dense.LatentSemantic``) answering the context-selection cases, delegated, not copied
(see ``hub_seat.py``). No model call; the dense arms embed with the local bge-small model. Keeps no cards, so the store charge is zero."""
from hubofhubs.retrieval.dense import LatentSemantic
from context_selection.hub_seat import build_arm

ALTERNATIVE = True
build = build_arm(LatentSemantic, learned=False)
