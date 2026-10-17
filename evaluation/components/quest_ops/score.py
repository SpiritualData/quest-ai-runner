"""Score one quest-operation case by the state of the world afterwards. ``expect`` is a list of checks:
  {"tool": name, "args_contain": {arg: substring}}  -> exactly such a write happened (once)
  {"no_writes": true}                               -> nothing was written at all
  {"state_contains": {"current_state": "58"}}       -> a state field still/now contains text
Also fails on any write the case did not ask for, except tool names listed in the case's ``allowed_calls``
(a preview call that changes nothing, such as an unconfirmed delete, which the state check then pins)."""
from quest_ops.fake_quest import WORLD


def score_case(fn, case):
    WORLD.reset()
    out = fn(case["message"], WORLD)
    writes = WORLD.mutations()
    allowed = set(case.get("allowed_calls", []))
    writes = [w for w in writes if w[0] not in allowed]
    problems, used = [], set()
    for chk in case["expect"]:
        if chk.get("no_writes"):
            if writes:
                problems.append(f"wrote {[n for n, _ in writes]} but nothing was asked to change")
        elif "tool" in chk:
            hit = None
            for i, (n, a) in enumerate(writes):
                if n == chk["tool"] and i not in used and all(
                        sub.lower() in str(a.get(k, "")).lower() for k, sub in chk.get("args_contain", {}).items()):
                    hit = i
                    break
            if hit is None:
                problems.append(f"no {chk['tool']} with {chk.get('args_contain', {})}")
            else:
                used.add(hit)
        elif "state_contains" in chk:
            for k, sub in chk["state_contains"].items():
                if sub.lower() not in str(WORLD.state.get(k)).lower():
                    problems.append(f"state[{k}] lacks {sub!r}")
    extra = [n for i, (n, _) in enumerate(writes) if i not in used]
    if extra and not any(c.get("no_writes") for c in case["expect"]):
        problems.append(f"unrequested writes: {extra}")
    return dict(ok=not problems, got=",".join(n for n, _ in writes) or "(no writes)",
                note="; ".join(problems) or (out.get("reply") or "")[:70].replace("\n", " "),
                reply=(out.get("reply") or "")[:400])
