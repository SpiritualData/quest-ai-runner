"""Answers the hub's retrieval cases with a QAR arm, so the hub's scorer (the foundation of this metric) measures it.

``metric-select-context`` builds on the hub's ``context_retrieval`` metric (``esoi.toml`` ``[builds_on.context_selection.retrieval]``): the hub owns
the file, topic, orientation and no-context cases and their scorer. An arm that IS a hub arm (``hub_<name>``) reuses the hub's recorded results.
An arm of QAR's own (``select(task, corpus) -> files``) has no hub result, so the hub's scorer runs it through this adapter: the hub gives a case,
the arm ranks the files of the hub's pinned corpus, and the first files that fit the hub's token budget are returned as hits. Nothing of the hub's
cases or scoring is copied here; the adapter only converts the arm's ranking into the hits the hub's scorer reads.

A file is shown whole up to what is left of ``mix.BUDGET_TOKENS`` (a token is four characters); the store the arm keeps is its cards per file
times the bytes of the corpus's cards, so an arm that keeps no cards pays no store penalty and a card per file pays the hub's full penalty.
"""
import json
import time

from context_selection.corpus import corpus_of

CHARS_PER_TOKEN = 4


def foundation_fn(name, select):
    """The function the foundation ``name``'s scorer calls for QAR's ``select``, or None for a foundation this arm cannot be adapted to
    (only the retrieval foundation answers requests with files; the bootstrap foundation measures card builders, which a selector is not)."""
    if name != "retrieval":
        return None
    state = {}

    def fn(case):
        from context_retrieval import mix
        from context_retrieval.score import world
        built = None
        if "corpus" not in state:
            started = time.time()
            pin = world()["pin"]
            state["corpus"] = corpus_of(pin.root, pin.files)
            state["card_bytes"] = len(json.dumps(list(state["corpus"].cards.values()), default=str))
            built = round(time.time() - started, 1)
        corpus, files = state["corpus"], world()["files"]
        ranked = select(case["task"], corpus)
        hits, left = [], mix.BUDGET_TOKENS
        for rel in dict.fromkeys(ranked):
            if rel not in files or left <= 0:
                continue
            tokens = min(max(len(files[rel]) // CHARS_PER_TOKEN, 1), left)
            hits.append(dict(file=rel, tokens=tokens, id=rel))
            left -= tokens
        per_file = getattr(select, "cards_per_file", None)
        per_file = 1.0 if per_file is None else per_file
        return dict(hits=hits, index_bytes=int(per_file * state["card_bytes"]), build=built, size=sum(len(t) for t in files.values()),
                    units=len(corpus.files), extra=0)
    return fn
