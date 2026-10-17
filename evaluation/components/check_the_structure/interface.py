"""The structure-check function. Every solution is ONE function with this signature:

    check(kind: str, args: dict) -> dict

``kind`` is ``repo`` (the repository's own structure registry) or ``consumer`` (a system built on QAR checking its
integration registry). ``args["edits"]`` is a list of declarative edits (see
``fixture.apply_edits``) that inject ONE known violation into a small synthetic fixture repo; an empty list
is the clean fixture. The function runs its checker over that fixture and returns

    {"exit": process exit code (0 clean, 1 violations), "problems": ["S4 path: message", ...]}

A solution is judged on MUTATION DETECTION: for each injected violation it must report the right rule on
the right path, and it must stay silent on the clean fixture (no false positive). No model, no network.
An implementation module in ``impls/`` exposes ``build(meter) -> check``.
"""
