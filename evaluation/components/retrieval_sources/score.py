"""Scoring is the retrieval component's, unchanged: recall@3 of the labelled documents and the answer phrase in the
text the adapter read. Re-exported so the per-source arm and the stack arm cannot score differently."""
from retrieval.score import aggregate, score_case  # noqa: F401
