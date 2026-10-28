"""The account-state function (the metric of ``act-as-the-body``). Every solution is ONE function:

    run_script(case: dict) -> {"handler_calls": [args, ...], "reads": int, "final_prompt": str}

A case is a scripted request over a tiny in-memory account (one ``status`` value) that the host exposes as a readable
document (``account.md``) and one writable operation (``set_status``). The case's ``script`` is the planner's
decisions, in order: ``{"read": true}`` reads the account document, ``{"write": "<value>"}`` calls ``set_status`` with
that value, ``{"write": "<value>", "fails": true}`` calls it and the operation reports failure (the account is not
changed), and ``{"answer": true}`` ends the request. The function runs the REAL Orchestrator over them and returns the
writes the account received, how many times the document was actually read, and the last planner prompt.

Scoring (``score.py``) reads those receipts, never any reply text: the writes that ran against the writes expected, the
reads that ran against the reads expected (a read after a change must run again; a read repeated with no change must
not), and the last prompt against the strings that must show the current state. A case marked ``known_gap`` asks for what
the solution's text says and no implementation does today.

An implementation module in ``impls/`` exposes ``build(meter) -> run_script``. No solution here calls a model.
"""
