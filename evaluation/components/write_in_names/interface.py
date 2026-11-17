"""The outward-text check. Every solution is ONE function with this signature:

    check(kind: str, args: dict) -> dict

An autopilot's standing instructions open with machine-read lines and its records carry ids; what it writes for people (email subject and body, task
title and text, goal text, quest notes, asks) must carry names instead. The function receives the texts one write would send and says whether the write
may go. It never rewrites: only the writer can name a record the way a person would.

Kind ``outward``, args: ``texts`` (list of strings: subject, body, title, ...).
Returns {"refused": the write must not go, "named": the record ids and machine header labels found (a list, in order of appearance, each once)}.

An implementation module in ``impls/`` exposes ``build(meter) -> check``. No solution here calls a model.
"""
