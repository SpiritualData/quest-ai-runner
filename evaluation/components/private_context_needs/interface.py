"""The private-context-needs function. Every solution is ONE function with this signature:

    select(task: str, candidates: list[dict]) -> list[str]

``task`` is a real request that was sent to an assistant. ``candidates`` are the items that existed around it (a note, a goal update,
a decision, a conversation turn, a quest state field, a context card ...), each ``{"item_id", "type", "text"}``. The labels are never
passed in. Returns the ``item_id`` values to put in front of the assistant. An empty list is a valid answer: some inputs are
self-contained and the right context is nothing.

The cases are PRIVATE real data and are never in this repository. They are read from the folder named by the environment variable
``QAR_PRIVATE_CONTEXT_DATASET`` (a folder holding ``cases.jsonl``, or that file itself). When the variable is absent or the file is
missing the component loads zero cases and says why, and every run is an empty run, not a failure.

An implementation module in ``impls/`` exposes ``build(meter) -> select``. None of the arms here makes a model call.
"""
