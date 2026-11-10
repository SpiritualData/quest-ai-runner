"""The digest-by-need function (the metric ``metric-digest-by-need`` of ``choose-the-context``). Every implementation is ONE function:

    digest(case: dict) -> {"cited": list, "context_chars": int, "max_prompt_chars": int, "calls": int, "rerun_calls": int, "statuses": dict}

A case describes a body of records with ``fixtures.groups_for`` (collections of generated entries, a few of which carry the marker the stub model
cites), a ``need`` and, for the failure case, the group the stub model cannot read. The function reads the body for the need, then reads it again with
the cache the first read filled, and reports what reached the prompt, the largest prompt a model call was given, the model calls of each read and the
status of every group. The model is a stub that records its calls: no implementation spends anything.

Scoring (``score.py``): the ids cited are exactly the ones expected, the context stays within its cap, no model call is given a prompt over its cap, the
second read makes no call, and a failed group is reported without failing the read.

An implementation module in ``impls/`` (or ``broken/``) exposes ``build(meter) -> digest``.
"""
