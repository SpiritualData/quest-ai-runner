"""The overseer-signal function. Every solution is ONE function with this signature:

    signal(digest: str) -> {"signal": str | None, "reason": str, "hint": str}

``digest`` is the compact run snapshot ``core.overseer.build_digest`` renders at hook A (plan loop, no
draft yet) or hook B (final checkpoint, draft present). A solution returns one of ``proceed``, ``redirect``,
``answer_now``, ``escalate_deep``, ``escalate_human``, or ``None`` when it makes no judgement.

Two arms, scored on the same 16 hand-labelled scenarios (ported from the retired standalone script):

* ``digest_probe`` (offline, recorded): returns ``None``. The scorer then checks only that the digest
  carries the evidence the label depends on (the draft, the refused proposal, the recent corrections, the
  operations) inside the 1600-char budget. A judge cannot be right about what it was never shown.
* ``live_model`` (needs a model, waived in waivers.csv S10): the real ``oversee()`` call at the harness
  model (``QAR_EVAL_MODEL``). Scored on the signal against ``want``.

Rules the scenarios keep, so a pass is judgement and not phrase matching: scenario wording never reuses
the example phrasings in ``OVERSEER_PROMPT`` (no exemplar echo), and the decisions with a failure mode in
both directions are CONTRAST PAIRS (same request, only the evidence flips): OVS-007/008 (a draft that
describes the fix vs reports it done), OVS-009/010 (an offer in place of an answer vs a real answer),
OVS-012/013 (re-asking a declined proposal vs proposing what the user now asked for), OVS-014/015 (a
question that mentions an action verb vs the same words as an instruction).
"""
