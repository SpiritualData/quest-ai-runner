"""The measurement function. Every solution is ONE function with this signature:

    measure(kind: str, args: dict) -> dict

A repo decides which implementation to run by recorded numbers, so the harness that records and compares them
has to be right: the winner is the higher pass rate (equal rate: lower cost), scores on different case files
are never compared, a row with no provenance is flagged, a tie is not a win, a record carries the commit and
the case file's hash, and a run budget is enforced. The cases drive the LIVE ``leaderboard.py``, ``common.py``
and ``run.py`` against synthetic ``results.csv`` and ``solutions.csv`` tables and a synthetic mini component
built in a temporary folder (with its own throwaway git repository). Deterministic and offline.

Kinds and the dict a solution returns:
  rank      args {rows: [{impl, passed, cases, cost_usd, cases_sha}], selected}
            -> {"ranked": [impl, best first], "beats": [challenger impls], "unverified": [impls]}
  board     args {rows: [full results.csv rows], selected, strict}
            -> {"exit": int, "text": leaderboard output, "beats": [...], "incomparable": [...]}   (``main`` end to end)
  record    args {cases_file: "json" | "csv", dirty: bool, edit_cases: bool}
            -> {"git_sha": bool, "dirty": bool, "cases_sha_matches": bool, "cases_sha_changes": bool, "rows": n}
  run       args {impl, argv: [extra flags]} -> {"cases": n, "passed": n, "error": exception name | None}
            the shared ``run_component`` driver on the mini component (``--only``, ``--max-calls``)
  validity  args {} -> {"cases": n, "good_passed": n, "bad_passed": n}
            a component is valid offline when a reference good implementation passes every case and a reference
            bad one fails every case
An implementation module in ``impls/`` exposes ``build(meter) -> measure``. No solution here calls a model.
"""
