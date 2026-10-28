"""Score one goal hand-off verdict against the label its source states, and weigh value against cost and time.

Per case: ``ok`` (the verdict is the labelled one: for an assignee case the person and the doability both), ``wrong_yes`` (the goal
would be handed to the AI representative although the label says a person must keep it: the costly error, the prompt says a wrong yes
costs the person a goal they must take back), ``calls`` (model calls, put there by the runner).

``aggregate`` reads the whole run: share correct (value, a proxy for the verdicts a person would not reverse), wrong yes count, model
calls per case (cost, with dollars and tokens in the results row) and the calls on the critical path per case (time: both judges run
inside goal creation or the autopilot pass and are awaited)."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_cases():
    return json.loads((HERE / "cases.json").read_text(encoding="utf-8"))


def score_case(fn, case):
    got = fn(case)
    kind, label = case["kind"], case["label"]
    if kind == "doability":
        have, want = got["ai_can_do"], label["ai_can_do"]
        ok = have is want
        return dict(ok=ok, got=str(have), want=str(want), wrong_yes=bool(have and not want), kind=kind,
                    note="ok" if ok else f"ai_can_do {have}, labelled {want}")
    have_who, want_who = got.get("assignee"), label["assignee"]
    have_ai, want_ai = bool(got.get("ai_can_do")), label["ai_can_do"]
    problems = []
    if have_who != want_who:
        problems.append(f"assignee {have_who}, labelled {want_who}")
    if have_ai is not want_ai:
        problems.append(f"ai_can_do {have_ai}, labelled {want_ai}")
    return dict(ok=not problems, got=f"{have_who}/{have_ai}", want=f"{want_who}/{want_ai}", wrong_yes=bool(have_ai and not want_ai),
                kind=kind, note="ok" if not problems else "; ".join(problems))


def aggregate(rows):
    n = len(rows) or 1
    ok = sum(1 for r in rows if r.get("ok"))
    calls = sum(r.get("calls", 0) for r in rows)
    wrong_yes = sum(1 for r in rows if r.get("wrong_yes"))
    by_kind = {}
    for r in rows:
        k = by_kind.setdefault(r.get("kind", "?"), [0, 0])
        k[0] += 1
        k[1] += 1 if r.get("ok") else 0
    parts = ", ".join(f"{kind} {v[1]}/{v[0]}" for kind, v in sorted(by_kind.items()))
    return (f"value {ok}/{n} verdicts as labelled ({parts}); wrong yes (a goal handed to the AI that a person must take back) {wrong_yes}, "
            f"so {n - wrong_yes}/{n} verdicts a person would not reverse; "
            f"cost {calls / n:.2f} model calls per case, all awaited inside goal creation or the autopilot pass; labels are source "
            f"fixtures, not person reassignments")
