"""Score one case against its labelled expectation (``esoi.expect``); ``aggregate`` reports passes per case kind.

The 20 offline cases are ``cases.json``. The real-turn cases (``real_cases.json``) join only when ``QAR_EVAL_LIVE_TURNS=1``, because each
one runs a real turn on the Claude Code subscription; a run made that way has a different case set, so it never overwrites an offline row."""
import json
import os
from pathlib import Path

from esoi import expect

HERE = Path(__file__).resolve().parent


def load_cases():
    if os.environ.get("QAR_EVAL_REAL_CASES"):   # the second real tier alone: its own frozen file, the turns run only with the live switch
        return json.loads((HERE / os.environ["QAR_EVAL_REAL_CASES"]).read_text(encoding="utf-8"))
    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    if os.environ.get("QAR_EVAL_LIVE_TURNS") == "1":
        cases = cases + json.loads((HERE / "real_cases.json").read_text(encoding="utf-8"))
    return cases


def score_case(fn, case):
    return expect.score_kind_case(fn, case)


def aggregate(rows):
    return expect.aggregate_by_kind(rows)
