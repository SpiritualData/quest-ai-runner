"""The combined QAR metric's function. Every implementation is ONE function:

    read(args) -> reading

``args`` is one sub-metric's definition from ``cases.json`` (its normalisation ``rule`` and the recorded arms of its
``cheap`` and ``full`` tiers, planned arms included). The reading (``combine.read``) holds, per tier, every arm's latest
row in that tier, whether it is current, its value in 0..1, its date, commit and cost, and the tier's value over its
current arms with the share that is measured. An implementation decides which rows count; it never runs an arm, so the
metric costs nothing and is computed from recorded rows only.

The scorer applies the stated rule independently: a reading that counts a stale, missing or planned arm as measured fails
its case, and so does a sub-metric whose quick tier is not fully measured or falls below the bar in ``esoi.toml``. The
cases passed are the sub-metrics measured on current evidence and at or above the bar; the floor, measured weight, headline and
ceiling are the report ``aggregate`` prints. An implementation module in ``impls/`` exposes ``build(meter) -> read``.
"""
