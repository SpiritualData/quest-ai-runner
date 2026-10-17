"""The deep-run liveness review function. Every solution is ONE function with this signature:

    review(elapsed: int, idle: int, tail: str) -> {"stop": bool | None, "reason": str}

``tail`` is the end of a deep worker's live session log, ``elapsed`` the seconds the run has gone, ``idle``
the seconds since the log last changed. The answer is the watchdog's one binary question: should this worker
STILL be running? ``stop`` is True only for idle past 5 minutes with no legitimate reason, a token or usage
limit, a block on a human, or the same complete action repeated; slow-but-alive work (builds, tests,
monitoring, downloads) must come back False, because a wrong stop loses work that was about to land.
``None`` means no verdict (production then leaves the run alone).

39 hand-labelled logs, ported from the retired standalone script (20 keep-running, 19 stop). Two arms:

* ``rule_floor`` (offline, recorded): a keyword-and-threshold rule, no model. The floor a model must beat.
* ``live_reviewer`` (needs the ``claude`` CLI, waived in waivers.csv S10): the production prompt, parser
  and one-shot ``claude -p`` call (``SubprocessGoalRunner._call_reviewer_cli``) at the review model.

Labels follow the rule in ``DEEP_REVIEW_PROMPT``; the rule-floor score therefore measures how much of the
judgement a threshold can carry, not how good the production reviewer is. The rule floor was written with
these cases in view, so its score is an optimistic ceiling for a rule (there is no held-out set here), not a
figure to compare a model against as if it were blind.
"""
