"""Shared machinery of the arms: the node bridge into quest-frontend, the variants of one turn, and the checks by code."""
import json
import os
import re
import subprocess
from pathlib import Path

import common  # noqa: F401  (puts the repository on the path)
from work_trace_check import actual_totals, check_turn, latest_by_step

HERE = Path(__file__).resolve().parent
#: The frontend checkout that holds the Trail rules; a sibling of this repository unless QUEST_FRONTEND_DIR names another.
FRONTEND = Path(os.environ.get("QUEST_FRONTEND_DIR") or common.REPO.parent / "quest-frontend")
BRIDGE = FRONTEND / "scripts" / "run-worktrace-view.js"
PRIVATE_KINDS = ("records", "action")
JARGON = re.compile(r"\b(step|steps|arm|solution|seat|ledger|tier|tool call|trace_id|step_id)\b", re.I)
URLISH = re.compile(r"https?://|www\.|\?[a-z_]+=|(?:^|\s)(?:/|~/)[\w.-]+/")


def bridge(arm, variants):
    if not BRIDGE.exists():
        raise SystemExit(f"the frontend bridge is missing: {BRIDGE} (set QUEST_FRONTEND_DIR to a quest-frontend checkout)")
    out = subprocess.run(["node", str(BRIDGE)], input=json.dumps({"arm": arm, "variants": variants}), capture_output=True, text=True,
                         cwd=str(FRONTEND), timeout=120)
    if out.returncode != 0:
        raise RuntimeError("bridge failed: " + (out.stderr or out.stdout)[-400:])
    return json.loads(out.stdout)


def projected(events):
    """What the hub sends other participants of a shared thread: record and action steps carry counts only."""
    out = []
    for e in events:
        e = dict(e)
        if e.get("kind") in PRIVATE_KINDS:
            e["items"] = []
        out.append(e)
    return out


def stored_of(events):
    """What is saved on the reply: the latest event of each step, in order of first appearance, terminal steps only."""
    latest = latest_by_step(events)
    order = []
    for e in events:
        if e["step_id"] not in order:
            order.append(e["step_id"])
    return [latest[s] for s in order if latest[s].get("state") in ("done", "failed")]


def variants_for(case):
    events = case["events"]
    replay = events + events[-3:]          # a resumed stream repeats earlier frames
    if case.get("surface") == "group":
        shared_events, shared_stored = projected(events), projected(stored_of(events))
        return [dict(name="sender_live", events=replay, shared=True), dict(name="sender_history", stored=shared_stored, shared=True),
                dict(name="other_live", events=projected(replay), shared=True), dict(name="other_history", stored=shared_stored, shared=True)]
    return [dict(name="sender_live", events=replay, shared=False), dict(name="sender_history", stored=stored_of(events), shared=False)]


def views_for(arm, case):
    return bridge(arm, variants_for(case))


def totals(case):
    t = actual_totals(case["calls"])
    removed = sum(1 for c in case["calls"] if c.get("kind") == "links" for v in c.get("verdicts") or []
                  if str((v or {}).get("verdict") if isinstance(v, dict) else v).upper() != "OK")
    return {"web_read": t["web_read"], "web_found": t["web_found"], "papers_read": t["papers_read"], "papers_found": t["papers_found"],
            "records": t["records"], "links_checked": t["links"], "links_removed": removed}


def has_work(case):
    """The turn did something the person could be shown: a search, a read, records used, a link left out, or a slowdown they felt."""
    t = totals(case)
    felt = any(e.get("kind") == "notice" and e.get("affects_user") for e in case["events"])
    return felt or any(t[k] for k in ("web_read", "web_found", "papers_read", "papers_found", "records", "links_removed"))


def shown_elements(view, moments=("live", "settled", "expanded")):
    return [e for m in moments for e in view.get(m, [])]


def gate_problems(case, views):
    """Truthfulness: the emitted events, then every element shown to every viewer."""
    shared = case.get("surface") == "group"
    problems = list(check_turn(case["calls"], case["events"], shared=False, private_titles=(), reply=case.get("reply")))
    if shared:
        problems += [p for p in check_turn(case["calls"], projected(case["events"]), shared=True, private_titles=case.get("private_titles") or (), reply=case.get("reply"))]
    actual = totals(case)
    for name, view in views.items():
        viewer_shared = shared and name.startswith("other")
        for el in shown_elements(view):
            for claim in el.get("counts") or []:
                if claim["count"] > actual.get(claim["counted"], 0):
                    problems.append(f"{name}: shows {claim['count']} for {claim['counted']} but the run did {actual.get(claim['counted'], 0)}")
            text = el.get("text", "")
            if viewer_shared and any(t and t in text for t in case.get("private_titles") or ()):
                problems.append(f"{name}: a private title is shown to another participant")
            if URLISH.search(text):
                problems.append(f"{name}: a URL, query or path is shown: {text[:50]}")
            if JARGON.search(text):
                problems.append(f"{name}: internal word shown: {text[:50]}")
            if any(d in text for d in ("—", "–")):
                problems.append(f"{name}: a dash is shown")
    return problems


def parity_problems(case, views):
    """The settled and expanded views agree across viewers and across live and history."""
    keys = ("settled", "expanded")
    base = None
    problems = []
    for name, view in views.items():
        sig = {k: [e["text"] for e in view.get(k, [])] for k in keys}
        if base is None:
            base_name, base = name, sig
        elif sig != base:
            problems.append(f"{name} differs from {base_name}")
    return problems
