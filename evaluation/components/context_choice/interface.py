"""The context-choice function (the metric of ``choose-the-context``). Every implementation is ONE function:

    choose(case: dict) -> {"context": str, "resolved": str, "found": list, "searched": list}

A case scripts the four members of the area: ``task`` and the ``context`` the lane's assembler returns for it, a ``locator`` and the
text ``resolved`` for it, a ``query`` and the ``hits`` the meaning index returns, and the ``views`` the mixed search ranks (none: an empty
list). The function builds the area's arm over those scripted members and returns what each line handed back.

Scoring (``score.py``): each line must return exactly what the member that owns it returned. No implementation calls a model.

An implementation module in ``impls/`` exposes ``build(meter) -> choose``.
"""
