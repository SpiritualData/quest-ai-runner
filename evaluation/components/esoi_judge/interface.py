"""The ESOI judge function. Every solution is ONE function with this signature:

    grade(case: dict) -> {"verdict": "pass" | "fail", "reason": str}

``case["case_id"]`` names a case in ``esoi_behavior/cases.csv`` and ``case["side"]`` says which of its two
reference replies to grade (``good`` is the ``good_reference``, ``bad`` the ``bad_reference``); the function
receives the case already joined to that reply as ``case["reply"]`` and the behaviour case as
``case["behavior_case"]``. The label (``case["expected"]``, pass for good, fail for bad) is the answer key.

What this measures: how often a judge agrees with labelled replies. It is the judge's OWN metric, separate
from the behaviour scores it is used for. Today the labels come from the case author (the references were
written with the cases); calibration against Joshua's verdicts (``reviewer_verdict`` in the sheet) is pending
his review, and until then agreement shows the judge is not broken, not that it is right.
"""
