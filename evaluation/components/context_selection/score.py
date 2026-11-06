"""Deterministic scoring of the cases only QAR can measure. No model judge.

``metric-select-context`` BUILDS ON the hub's ``context_retrieval`` metric (``esoi.toml`` ``[builds_on.context_selection.*]``, the esoi principle that a
consumer's metric measures on its foundation without re-running it). The hub owns the file-finding, topic, orientation and no-context kinds, their
corpus and their scorer; an arm that is a hub arm (``hub_<name>``) reuses the hub's recorded per-case results when they are current, and any other arm
is run through the hub's own scorer by ``foundation_adapter.py``. When the hub adds a case kind or fixes its scorer, this number moves with it and
nothing is ported here.

What stays here is what the hub cannot see: the private source (``private_cases.py``), real requests with labelled candidate items, read from
``QAR_PRIVATE_CONTEXT_DATASET`` when it is set and contributing nothing when it is not (the number is then the foundation's alone). The arm builds cards
over the case's candidate items and the first few it ranks are the selection. The quest scope modes (``QAR_PRIVATE_SCOPE_MODE``) apply to this source only.

The shares of the one number are written in ``esoi.toml`` (``share`` of each foundation; the private source carries the rest), and
``[balance.context_selection]`` folds accuracy, cost (with the store charge of ``store_cost.py``, the size of the context a private case selects and
the priced source reads) and time into the one balanced number every arm is ranked by."""
from context_selection import private_cases
from context_selection.foundation_adapter import foundation_fn  # noqa: F401  (esoi.foundation looks for it in this module)


def load_cases():
    """Only QAR's own cases: the private ones, when the dataset is present. The hub's cases are the foundation's and are not copied."""
    return private_cases.load_cases()


def score_case(fn, case):
    return private_cases.score_case(fn, case)


def aggregate(rows):
    private = [r for r in rows if r.get("private")]
    if not private:
        return "case sources private 0 (dataset absent); the hub's cases are the foundation's"
    passed = sum(bool(r["ok"]) for r in private)
    loaded = len(private_cases.load_all(private_cases.present_folders()))
    ran = f"{len(private)} of {loaded} loaded (a run of only the cases that still fail scores a part)" if len(private) < loaded else str(len(private))
    return (f"case sources private {ran} | real inputs {passed}/{len(private)} ({passed / len(private):.0%}), "
            f"mean accuracy {sum(r['accuracy'] for r in private) / len(private):.3f}")
