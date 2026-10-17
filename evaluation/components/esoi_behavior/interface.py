"""The ESOI behaviour function. Every solution is ONE function with this signature:

    respond(case: dict) -> str

``case["scenario"]`` is the message a person sends an AI assistant; the function returns the assistant's
reply. A solution must use only ``scenario`` (the other columns are the answer key). The scorer then checks
the reply against the case's ``must_have`` regexes (all must match) and ``must_not`` regexes (none may
match), both case-insensitive.

What this measures: whether an AI follows the evolvable-structure (ESOI) principles in its own work, one
principle per case (P1..P13, see docs/structure/principles.csv), including cases where adding structure
is the wrong answer (P1, P12). The dataset is ``cases.csv`` (one row per scenario, built to be reviewed as a
Google Sheet; the ``reviewer_*`` columns are the human's).

Offline validity (no model, no cost): ``reference_good`` returns each case's ``good_reference`` and must
pass every case; ``reference_bad`` returns ``bad_reference`` and must fail every case. That proves the
checks can tell ESOI behaviour from naive behaviour. The live arms (``standing_instructions`` and the
no-instructions baseline ``plain``) make one model call per case and are run only when someone decides to
pay for them.
"""
