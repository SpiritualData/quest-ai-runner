"""Score one terminal-helper case against the label its source states, and weigh value against cost and time.

Per case: ``ok`` (the helper's result is the labelled one), ``calls`` (model calls, put there by the runner),
``wrong_route`` (a result a person would act on that is wrong: a classified ask with the wrong kind, an AI-doable
verdict that is wrong, an acknowledgement that breaks the prompt's own contract), ``applied`` (a classification the
planner would apply after its own checks).

``aggregate`` reads the whole run, not one case: share correct (value), model calls and estimated tokens and dollars per
case (cost), and calls on the critical path per case (time: every helper call here is awaited before the command
prints or continues, so the critical-path count equals the call count)."""
from assist_a_terminal_session import arms


def contract_problems(text):
    """Breaks of the ack prompt spec found in printed text: empty, a dash, more than 20 words."""
    problems = []
    if not text.strip():
        problems.append("empty")
    if any(d in text for d in arms.EM_DASHES):
        problems.append("dash")
    if arms.words(text) > arms.MAX_ACK_WORDS:
        problems.append(f"{arms.words(text)} words")
    return problems


def score_case(fn, case):
    got = fn(case)
    kind = case["kind"]
    if kind == "ack":
        want = case["expect"]
        stdout = got["stdout"]
        problems = []
        if got["exit"] != want["exit"]:
            problems.append(f"exit {got['exit']}, labelled {want['exit']}")
        if want.get("stdout_starts") and not stdout.startswith(want["stdout_starts"]):
            problems.append(f"printed {stdout[:50]!r}, labelled to start {want['stdout_starts']!r}")
        if want.get("contract"):
            problems += contract_problems(stdout)
        if want.get("max_calls") is not None and got.get("provider_requests", 0) > want["max_calls"]:
            problems.append(f"asked for a model {got['provider_requests']} time(s) on a task that was not queued")
        return dict(ok=not problems, got=stdout[:60], want="ack", wrong_route=bool(problems and want.get("contract")),
                    note="ok" if not problems else "; ".join(problems), kind=kind)
    if kind == "classify":
        raw = got.get("raw") or {}
        have = str(raw.get("kind") or "").strip().lower()
        want = case["label"]["kind"]
        ok = have == want
        return dict(ok=ok, got=have or "no answer", want=want, wrong_route=bool(have and not ok),
                    applied=bool(got.get("decision")), kind=kind, note="ok" if ok else f"{have or 'no answer'}, labelled {want}")
    if kind == "judge":
        raw = got.get("raw") or {}
        have = raw.get("ai_doable")
        want = case["label"]["ai_doable"]
        ok = (have is True) == want   # no verdict leaves the goal with its owner, which is the right outcome for a goal that is not AI work
        return dict(ok=ok, got=str(have), want=str(want), wrong_route=have is not None and not ok, kind=kind,
                    note="ok" if ok else f"{have}, labelled {want}")
    sent, want = got["sent"], case["expect"]["sent"]
    return dict(ok=sent == want, got=",".join(sent), want=",".join(want), wrong_route=False, kind=kind,
                note="ok" if sent == want else f"sent {sent}, labelled {want}")


def aggregate(rows):
    n = len(rows) or 1
    ok = sum(1 for r in rows if r.get("ok"))
    calls = sum(r.get("calls", 0) for r in rows)
    wrong = sum(1 for r in rows if r.get("wrong_route"))
    applied = sum(1 for r in rows if r.get("applied"))
    by_kind = {}
    for r in rows:
        k = by_kind.setdefault(r.get("kind", "?"), [0, 0, 0])
        k[0] += 1
        k[1] += 1 if r.get("ok") else 0
        k[2] += r.get("calls", 0)
    parts = ", ".join(f"{k} {v[1]}/{v[0]} ({v[2]} calls)" for k, v in sorted(by_kind.items()))
    return (f"value {ok}/{n} correct ({parts}); wrong routes {wrong}; classifications the planner would apply {applied}; "
            f"cost {calls / n:.2f} model calls per case, all on the critical path (time proxy {calls / n:.2f} awaited calls per case)")
