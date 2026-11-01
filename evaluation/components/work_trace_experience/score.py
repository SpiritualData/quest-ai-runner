"""Score one finished turn for one arm: truthfulness gate, parity, coverage, and (on real turns) the value judge.

Per case: ``ok`` (the turn is passed), ``gate`` (problems of truthfulness), ``parity`` (viewers that differ), ``shown`` (elements in the
settled and expanded views plus the live line), ``earned`` (elements the judge says earn their place, real turns only), ``covered`` (the
turn had work and something was shown, or had none and nothing was). ``aggregate`` reads the whole run."""
import claude_judge
from work_trace_experience import arms

JUDGE_RULE = (
    "An element earns its place when it tells the person something they can use: it changes what they do next (open a source, "
    "check a record, doubt or trust a result, ask a follow-up), or it builds justified confidence that the answer rests on real work "
    "done for THIS question. It does not earn its place when it is decoration, repeats another element, counts internal work, lists "
    "things the answer did not use, or says nothing the person could act on or believe more because of.")


def judge_prompt(case, elements):
    lines = "\n".join(f"{i + 1}. [{e['group']}] {e['text']}" for i, e in enumerate(elements))
    return (
        "A person asked an AI assistant a question. Beside its reply, the app showed the elements listed below about the work the AI did. "
        "Judge each element separately.\n"
        f"The person's question: {case['message']}\n"
        f"The AI's reply (shortened): {case['reply'][:900]}\n"
        f"Elements shown:\n{lines}\n\n{JUDGE_RULE} Judge only against the question and the reply; use the element's own words, not what "
        "it might mean. A list heading that only introduces the rows under it earns its place if the rows do.\n"
        'Reply with {"verdicts": [{"n": 1, "earns": true or false, "why": "five words"}, ...]} covering every element.')


def unique(elements):
    seen, out = set(), []
    for e in elements:
        key = (e["group"], e["text"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def score_case(fn, case):
    got = fn(case)
    views = got["views"]
    gate = arms.gate_problems(case, views)
    parity = arms.parity_problems(case, views)
    sender = views["sender_live"]
    elements = unique(arms.shown_elements(sender))
    work = arms.has_work(case)
    covered = bool(sender["settled"]) == work
    row = dict(kind=case["kind"], stage=case["stage"], gate=gate, parity=parity, shown=len(elements), covered=covered, earned=None,
               got=case["kind"], want="a truthful, consistent view that shows only what earns its place")
    notes = []
    if gate:
        notes.append("gate: " + "; ".join(gate[:2]))
    if parity:
        notes.append("parity: " + "; ".join(parity[:2]))
    if not covered:
        notes.append("work was done and nothing is shown" if work else "nothing to show but something is shown")
    judged_ok = True
    if case["stage"] == "real" and elements and got.get("provider") is not None:
        verdict = claude_judge.ask_json(got["provider"], judge_prompt(case, elements))
        marks = {v.get("n"): v for v in verdict.get("verdicts") or [] if isinstance(v, dict)}
        if "error" in verdict or len(marks) < len(elements):
            judged_ok = False
            notes.append("judge: " + str(verdict.get("error", "incomplete verdicts")))
        else:
            earned = [e for i, e in enumerate(elements) if marks.get(i + 1, {}).get("earns") is True]
            row["earned"] = len(earned)
            for i, e in enumerate(elements):
                if marks.get(i + 1, {}).get("earns") is not True:
                    notes.append(f"does not earn its place: [{e['group']}] {e['text'][:50]} ({str(marks.get(i + 1, {}).get('why', ''))[:40]})")
            judged_ok = len(earned) == len(elements)
    elif case["stage"] == "real" and not elements and work:
        row["earned"] = 0
    ok = not gate and not parity and covered and judged_ok
    return dict(row, ok=ok, note="ok" if ok else "; ".join(notes))


def frontend_version():
    """The quest-frontend commit whose rules were measured (the bridge reads that checkout, which the row's own commit does not pin)."""
    import subprocess
    try:
        sha = subprocess.run(["git", "-C", str(arms.FRONTEND), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=20).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(arms.FRONTEND), "status", "--porcelain", "--", "src/utils/workTrace.ts", "src/components/chat/ai/WorkTrace.tsx",
                                "src/i18n/locales/en/quests.json", "scripts/run-worktrace-view.js"], capture_output=True, text=True, timeout=20).stdout.strip()
        return f"{sha}{'-dirty' if dirty else ''}"
    except Exception:  # noqa: BLE001
        return "unknown"


def aggregate(rows):
    n = len(rows) or 1
    gate_failed = sum(1 for r in rows if r.get("gate"))
    parity_failed = sum(1 for r in rows if r.get("parity"))
    uncovered = sum(1 for r in rows if not r.get("covered"))
    judged = [r for r in rows if r.get("earned") is not None]
    shown = sum(r["shown"] for r in judged)
    earned = sum(r["earned"] for r in judged)
    value = f"value {earned}/{shown} shown elements earn their place on {len(judged)} real turns" if judged else "value not judged (no real turn run)"
    by_kind = {}
    for r in rows:
        k = by_kind.setdefault(r.get("kind", "?"), [0, 0])
        k[0] += 1
        k[1] += 1 if r.get("ok") else 0
    parts = ", ".join(f"{kind} {v[1]}/{v[0]}" for kind, v in sorted(by_kind.items()))
    return (f"passed {sum(1 for r in rows if r.get('ok'))}/{n} turns ({parts}); truthfulness gate failed on {gate_failed}; parity broken on "
            f"{parity_failed}; work done and nothing shown, or the reverse, on {uncovered}; {value}; quest-frontend {frontend_version()}")
