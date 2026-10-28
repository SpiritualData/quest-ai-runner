"""Score one multi-step request by the receipts in the in-memory quest (see ``interface.py``).
A case may seed the world beyond the default (``seed_goals``, ``seed_experiments``) so a loop of reads has something to read.

``expect`` is a list of checks over the world's mutating calls (every call except the read-only finder):
  {"writes": tool, "args_contain": {arg: text}, "count": n}   exactly n such writes (default 1)
  {"forbid": tool, "args_contain": {arg: text}}              no such write at all (any args when none given)
  {"arg_startswith": {"tool": tool, "arg": arg, "prefix": text}}  every such write's arg starts with the prefix
  {"no_other_writes": true}                                  nothing was written beyond the checks above
  {"reply_forbidden": [regex]}                               the reply must not match (a claim the receipts contradict)
A failed check whose kind is ``forbid`` or ``arg_startswith`` is a CONTINUITY ERROR (earlier context was not honoured);
a missing or extra required write is a TASK error. The pass flag needs neither."""
import re

from quest_ops.fake_quest import WORLD

READ_PREFIXES = ("list_", "find_", "get_", "read_", "search_")   # read-only tools: never a write receipt


def has(args, wanted):
    return all(text.lower() in str(args.get(k, "")).lower() for k, text in wanted.items())


def score_case(fn, case):
    WORLD.reset()
    WORLD.state["goals"].extend(dict(g) for g in case.get("seed_goals", []))   # state the case says already exists
    WORLD.state["experiments"].extend(dict(e) for e in case.get("seed_experiments", []))
    out = fn(case, WORLD) or {}
    writes = [(n, a) for n, a in WORLD.mutations() if not n.startswith(READ_PREFIXES)]
    reply = out.get("reply") or ""
    steps = int(out.get("steps") or 0)
    continuity, task, used = [], [], set()
    for chk in case["expect"]:
        if "writes" in chk:
            hits = [i for i, (n, a) in enumerate(writes)
                    if n == chk["writes"] and has(a, chk.get("args_contain", {}))]
            used.update(hits)
            want = chk.get("count", 1)
            if len(hits) != want:
                task.append(f"{len(hits)} x {chk['writes']} {chk.get('args_contain', {})}, wanted {want}")
        elif "forbid" in chk:
            hits = [n for n, a in writes if n == chk["forbid"] and has(a, chk.get("args_contain", {}))]
            if hits:
                continuity.append(f"forbidden write happened: {chk['forbid']} {chk.get('args_contain', {})}")
        elif "arg_startswith" in chk:
            c = chk["arg_startswith"]
            for n, a in writes:
                if n == c["tool"] and not str(a.get(c["arg"], "")).startswith(c["prefix"]):
                    continuity.append(f"{n}.{c['arg']}={str(a.get(c['arg']))[:50]!r} lacks prefix {c['prefix']!r}")
        elif "reply_forbidden" in chk:
            for rx in chk["reply_forbidden"]:
                if re.search(rx, reply):
                    continuity.append(f"reply matches forbidden /{rx}/")
        elif chk.get("no_other_writes"):
            extra = [writes[i][0] for i in range(len(writes)) if i not in used]
            if extra:
                task.append(f"unrequested writes: {extra}")
    problems = continuity + task
    return dict(ok=not problems, continuity_errors=len(continuity), steps=steps,
                single_step=steps <= 1, tokens_in=out.get("tokens_in", 0), tokens_out=out.get("tokens_out", 0),
                got=",".join(f"{n}({str(a.get('title') or a.get('name') or a.get('value') or '')[:28]})"
                             for n, a in writes) or "(no writes)",
                note="; ".join(problems) or "receipts match", reply=reply[:300])


def aggregate(rows):
    steps = sum(r.get("steps", 0) for r in rows)
    errs = sum(r.get("continuity_errors", 0) for r in rows)
    multi = [r for r in rows if not r.get("single_step")]
    with_err = sum(1 for r in rows if r.get("continuity_errors"))
    return (f"pass {sum(bool(r['ok']) for r in rows)}/{len(rows)}; continuity errors {errs} over {steps} planner steps "
            f"= {round(100 * errs / steps, 1) if steps else 0} per 100 steps; requests with an error {with_err}; "
            f"multi-step requests {len(multi)}/{len(rows)} (a single-step request cannot show a step losing the request); "
            f"input tokens per request {round(sum(r.get('tokens_in', 0) for r in rows) / len(rows)) if rows else 0}")
