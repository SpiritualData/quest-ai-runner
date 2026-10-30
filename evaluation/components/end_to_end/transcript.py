"""Optional transcript of one end-to-end turn: what was asked, what the AI read, what it did and what it said.

Off by default. Setting ``QAR_E2E_TRANSCRIPTS=<directory>`` makes the ``qar_live`` arm write one small JSON per case into that
directory (scratch output, never committed). It only OBSERVES: it wraps the provider's ``plan`` method to copy each planner
decision, reads the world's own ordered log of tool calls and web searches, and reads the verdict from ``score.judge`` on the
turn that already ran. It makes no model call, changes no argument or result, and the scorer sees exactly the same turn.

A record holds: ``request``; ``steps`` (the planner's action and reason, then what that step read, searched or wrote); ``handoff``
(the goal and brief of a deep hand-off); ``context_used`` (stored records or documentation the person did NOT mention that the
turn drew on, inferred from where a write landed and from words the reply or the writes share with a record, so it is a hint
and says so); ``reply`` (cut) and ``reply_full`` (whole, what the rubric grades); ``verdict``; ``calls`` and ``cost_usd``. Long text is cut to a few hundred characters."""
import functools
import json
import os
import re
import tempfile
from pathlib import Path

from end_to_end import score

ENV = "QAR_E2E_TRANSCRIPTS"
CUT = 300
STOP = set("""about above after again also because before being between could does doing during each from have having here into
just more most much only other over same should since some such than that their them then there these they this those through
under until very what when where which while will with would your quest quests goal goals note notes today""".split())


def target_dir(env=None):
    """The directory transcripts go to, or None (the default) when none is asked for."""
    value = (os.environ if env is None else env).get(ENV, "").strip()
    return Path(value) if value else None


def cut(value, limit=CUT):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def short_args(args):
    return {k: cut(v, 120) if isinstance(v, str) else v for k, v in (args or {}).items()}


class DecisionLog:
    """Copies every planner decision a provider returns, with how long the world's trace was when the planner was called."""

    def __init__(self, world):
        self.world, self.items = world, []

    def attach(self, *providers):
        seen = set()
        for provider in providers:
            if provider is None or id(provider) in seen or getattr(provider, "decision_logged", False):
                continue
            seen.add(id(provider))
            original = getattr(provider, "plan", None)
            if original is None:
                continue
            provider.plan = self.wrap(original)
            provider.decision_logged = True

    def wrap(self, fn):
        @functools.wraps(fn)
        def logged(*args, **kwargs):
            schema = kwargs.get("tool_schema") or (args[2] if len(args) > 2 else None) or {}
            start = len(self.world.trace)
            out = fn(*args, **kwargs)
            if isinstance(schema, dict) and schema.get("name") == "decide" and isinstance(out, dict):
                self.items.append((start, dict(out)))
            return out
        return logged


def decision_step(decision):
    """The planner's choice in short form: action, reason, and the hand-off or question it carried."""
    step = {"action": str(decision.get("action") or "?"), "why": cut(decision.get("rationale") or "", 200)}
    goal = decision.get("goal") or (decision.get("deferred_deep") or {}).get("goal")
    brief = decision.get("deep_brief") or (decision.get("deferred_deep") or {}).get("brief")
    if step["action"] == "deep" or decision.get("deferred_deep"):
        step["handoff"] = {"goal": cut(goal or "", 240), "brief": cut(brief or "", 300)}
    if decision.get("confirm_question"):
        step["question"] = cut(decision["confirm_question"], 240)
    return step


def event_line(event):
    kind = event["kind"]
    if kind == "web":
        return {"type": "web search", "query": cut(event["query"], 160), "found": cut(event.get("out", ""), 200)}
    if kind == "read":
        return {"type": "read", "tool": event["tool"], "args": short_args(event["args"]), "found": cut(event.get("out", ""), 200)}
    return {"type": "write", "tool": event["tool"], "args": short_args(event["args"]), "quest_id": event.get("quest_id")}


def build_steps(world, decisions):
    """Pair each planner decision with the world events that ran before the next one."""
    steps = []
    for i, (start, decision) in enumerate(decisions):
        end = decisions[i + 1][0] if i + 1 < len(decisions) else len(world.trace)
        step = decision_step(decision)
        step["n"] = i + 1
        step["did"] = [event_line(e) for e in world.trace[start:end]]
        steps.append(step)
    if not decisions and world.trace:
        steps.append({"n": 1, "action": "tools", "why": "", "did": [event_line(e) for e in world.trace]})
    return steps


def tokens(text):
    """Distinctive words and numbers of a text: no stop words, no short numbers and no years (dates match everything)."""
    found = set()
    for t in re.findall(r"[a-z0-9][a-z0-9.,]*[a-z0-9]|[a-z0-9]", str(text).lower()):
        if t in STOP or re.fullmatch(r"(19|20)\d\d", t):
            continue
        if any(c.isdigit() for c in t):
            if len(t) >= 3:
                found.add(t)
        elif len(t) >= 5:
            found.add(t)
    return found


def records(world):
    """Everything the turn could have drawn on: the quests (outcome, state, goals, notes) and the documentation excerpts."""
    out = []
    for qid, q in world.quests.items():
        if world.docs_only:
            break
        out.append(dict(kind="quest", ref=qid, quest=q["name"], text=f"{q['name']}. Outcome: {q['outcome']}"))
        out.append(dict(kind="quest state", ref=qid, quest=q["name"], text=q["current_state"]))
        out += [dict(kind="goal", ref=qid, quest=q["name"], text=g["title"] + " due " + g["due"]) for g in q["goals"]]
        out += [dict(kind="note", ref=qid, quest=q["name"], text=n) for n in q["notes"]]
    out += [dict(kind="documentation", ref=d["id"], source=d["source"], title=d["title"], text=d["text"]) for d in world.docs]
    return out


def context_used(case, world, got, decisions):
    """Stored records the person did not mention that the turn used, strongest first. A quest counts when a write landed on it
    and the request did not name it; any record counts when the reply, the writes or the planner's text share at least two
    distinctive words (or one distinctive number) with it that the request lacks."""
    asked = tokens(case["message"])
    low_ask = case["message"].lower()
    produced = " ".join([got.get("reply", "")] + [json.dumps(w["args"], default=str) for w in got.get("writes", [])]
                        + [json.dumps(d, default=str) for _, d in decisions])
    made = tokens(produced) - asked
    landed = {w.get("quest_id") for w in got.get("writes", []) if w.get("quest_id")}
    used = []
    for rec in records(world):
        why, strength = "", 0
        if rec["kind"] == "quest" and rec["ref"] in landed and rec["quest"].lower() not in low_ask:
            why, strength = "the write landed on this quest, which the request did not name", 100
        else:
            shared = tokens(rec["text"]) & made
            numbers = [t for t in shared if any(c.isdigit() for c in t)]
            if len(shared) >= 2 or numbers:
                why, strength = "the reply or write uses: " + ", ".join(sorted(shared)[:4]), len(shared) + 5 * len(numbers)
        if why:
            item = {k: rec[k] for k in ("kind", "quest", "source", "title") if k in rec}
            item.update(text=cut(rec["text"], 160), why=why, inferred=True)
            used.append((strength, item))
    used.sort(key=lambda pair: -pair[0])
    return [item for _, item in used[:4]]


def build(case, world, got, decisions, calls, cost_usd):
    """The record of one turn, from the finished world, the turn's returned facts and the copied planner decisions."""
    steps = build_steps(world, decisions)
    handoffs = [s["handoff"] for s in steps if "handoff" in s]
    verdict = score.judge(got, case)
    return dict(
        case=case["id"], kind=case["kind"], side=case.get("side"), request=case["message"],
        pinned_quest=world.quests[world.pinned]["name"] if world.pinned in world.quests else None,
        docs_in_view=[d["source"] for d in world.docs], steps=steps, handoff=handoffs[0] if handoffs else None,
        context_used=context_used(case, world, got, decisions),
        writes=[{"tool": w["tool"], "args": short_args(w["args"]), "quest": (world.quests.get(w["quest_id"]) or {}).get("name")}
                for w in got.get("writes", [])],
        web=[cut(q, 160) for q in got.get("web", [])], route=got.get("route", ""),
        reply=cut(got.get("reply", ""), 700), reply_full=str(got.get("reply", "")), verdict=dict(ok=bool(verdict["ok"]), note=cut(verdict.get("note", ""), 240)),
        calls=calls, cost_usd=round(cost_usd, 6))


def save(directory, record):
    """One JSON per case, written whole or not at all."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{record['case']}.json"
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    os.replace(temp, final)
    return final


def record_turn(case, world, orchestrator, meter, run, directory):
    """Run ``run()`` (the turn) with the planner observed, save the transcript, and return the turn's own result unchanged."""
    log = DecisionLog(world)
    log.attach(getattr(orchestrator, "provider", None), *list((getattr(orchestrator, "providers", None) or {}).values())
               if isinstance(getattr(orchestrator, "providers", None), dict) else [])
    calls, cost = meter.calls, meter.cost_usd
    got = run()
    save(directory, build(case, world, got, log.items, meter.calls - calls, meter.cost_usd - cost))
    return got
