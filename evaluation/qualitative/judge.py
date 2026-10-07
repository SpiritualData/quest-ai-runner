"""Evidence building, deterministic pre-checks and the LLM judge for the qualitative eval.

Order of operations for one case (runner.py calls these):

    evidence = build_evidence(events_per_turn, tasks)      # what the chat visibly did
    pre      = precheck(case, evidence, after_snapshot, changes)   # deterministic, can fail outright
    verdict  = judge(case, evidence, changes, pre, ground_truth)   # LLM, via `claude -p`, sonnet

The deterministic layer exists because an LLM judge is the wrong tool for "did row X change" or "is
the number 2340 in the reply". It runs first, is cheap, is reproducible, and a HARD failure there
zeroes the case without spending a judge call (the judge is still run with ``--judge-always``).

The judge uses the stock ``claude -p`` CLI through the library's own ClaudeCliProvider (the same
keyless, subscription-billed path the deployed lanes use; the Anthropic API key has no credit), with
a family alias model (sonnet by default), no tools and no repo context.

Judge output is STRICT JSON, validated by ``normalise_verdict``:

    {"rubric": [{"id": 1, "item": "...", "pass": true, "evidence": "short quote"}],
     "score": 0.0-1.0,
     "routing_ok": bool, "routing_note": "...",
     "side_effects_ok": bool, "side_effects_note": "...",
     "context_used": [{"pivot": "NAME", "used": bool, "changed_answer": bool, "evidence": "..."}],
     "code_review": null | {"issues": [...], "verdict": "..."},
     "summary": "one line", "failure_class": null | "short_tag"}
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from devclient import DEV_ENV, REPO, event_name  # noqa: E402

sys.path.insert(0, str(REPO))

JUDGE_MODEL = "claude-sonnet"  # family alias; ClaudeCliProvider maps it to `--model sonnet`

# ---------------------------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------------------------

MAX_FIELD = 700


def clip(value, limit=MAX_FIELD):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + f"...[+{len(text) - limit} chars]"


WEB_STATUS = re.compile(r"searching the web|reading a web page", re.I)
WEB_SPEC = re.compile(r'"(?:web|web_page)"\s*:')


def web_read_fired(events):
    """True when a planner web read ran in this turn: a "Searching the web" / "Reading a web
    page" status tick, or a plan/read frame whose read spec carries a ``web`` or ``web_page``
    key. Deterministic, so a case can assert it (precheck ``web_read``) instead of guessing from
    links in the reply."""
    for e in events:
        name = event_name(e)
        if name == "status" and WEB_STATUS.search(str(e.get("text") or "")):
            return True
        if name in ("plan", "replan", "read") and WEB_SPEC.search(json.dumps(e.get("data") or {})):
            return True
    return False


def build_evidence(turns, tasks=None):
    """Condense the raw SSE frames of every turn into what a judge (or a human) needs.

    ``turns`` is a list of {"message": str, "events": [frame, ...]}. Frames are the wire frames of
    POST /api/quest-ai/conversations/{id}/messages/stream: ``plan`` (planner action per step),
    ``understanding`` (the resolved done-standard), ``read`` (a gather step: reads count + sources),
    ``context`` (context cards assembled: card_metadata, sources, counts), ``exec`` (execution
    lifecycle: phase code/executing/output/retry/done/error, tool, args, output), ``status`` ticks,
    ``token``/``partial``/``done`` (reply text), ``delegated`` (handed to an external environment
    as a task), ``explanation`` (the "Explain how I got this" trace: sections of what was read and
    done), ``tokens`` and ``mode_signal``.
    """
    out = {"turns": [], "tasks": [
        {"task_id": t.get("task_id") or t.get("id"), "text": clip(t.get("text"), 300),
         "status": t.get("status"), "goal_id": t.get("goal_id")} for t in (tasks or [])]}
    for turn in turns:
        events = turn["events"]
        names = [event_name(e) for e in events]
        done = [e for e in events if event_name(e) == "done"]
        reply = " ".join(str(e.get("content") or "") for e in done).strip()
        if not reply:
            reply = "".join(str(e.get("text") or "") for e in events
                            if event_name(e) in ("token", "result")).strip()
        plans = [{"phase": (e.get("data") or {}).get("phase"),
                  "step": (e.get("data") or {}).get("step"),
                  "action": (e.get("data") or {}).get("action"),
                  "text": clip((e.get("data") or {}).get("text"), 300)}
                 for e in events if event_name(e) in ("plan", "replan")]
        reads = [{"step": (e.get("data") or {}).get("step"),
                  "reads": (e.get("data") or {}).get("reads"),
                  "sources": clip((e.get("data") or {}).get("sources"), 500)}
                 for e in events if event_name(e) == "read"]
        contexts = []
        for e in events:
            if event_name(e) != "context":
                continue
            data = e.get("data") or {}
            cards = []
            for card in (data.get("card_metadata") or [])[:12]:
                cards.append({k: clip(v, 120) for k, v in (card or {}).items()
                              if k in ("id", "card_id", "title", "name", "kind", "type", "score",
                                       "quest_id", "source")})
            contexts.append({"counts": data.get("counts"), "cards": cards,
                             "sources": clip(data.get("sources"), 500)})
        execs = []
        for e in events:
            if event_name(e) != "exec":
                continue
            d = e.get("data") or {}
            execs.append({k: clip(v, 2500 if k == "code" else 600) for k, v in d.items()
                          if k in ("phase", "tool", "args", "arguments", "input", "output",
                                   "result", "error", "code", "attempt", "summary", "goal")})
        explanation = [e.get("data") for e in events if event_name(e) == "explanation"]
        tokens = [e.get("data") for e in events if event_name(e) == "tokens"]
        out["turns"].append({
            "message": turn["message"],
            "reply": reply,
            "frame_counts": {n: names.count(n) for n in sorted(set(names))},
            "understanding": [clip(e.get("text") or e.get("data"), 400)
                              for e in events if event_name(e) == "understanding"],
            "plans": plans,
            "reads": reads,
            "contexts": contexts,
            "execs": execs,
            "statuses": [clip(e.get("text"), 160) for e in events if event_name(e) == "status"][:25],
            "delegated": any(n == "delegated" for n in names),
            "delegated_task_ids": [e.get("task_id") for e in events
                                   if event_name(e) == "delegated" and e.get("task_id")],
            "explanation": clip(explanation[-1], 2500) if explanation else None,
            "tokens_final": (tokens[-1] if tokens else None),
            "errors": [clip(e, 300) for e in events
                       if event_name(e).startswith("_") or event_name(e) == "error"],
        })
    # Full generated code (the in-app chat acts by writing Python against Quest helpers, so the
    # "tool" is a call inside this text). Used by prechecks only; never sent to the judge whole.
    out["code_text"] = "\n".join(
        str(e.get("data", {}).get("code") or "") for t in turns for e in t["events"]
        if event_name(e) == "exec" and (e.get("data") or {}).get("phase") in ("code", "executing"))[:30000]
    out["reply"] = out["turns"][-1]["reply"] if out["turns"] else ""
    out["all_replies"] = [t["reply"] for t in out["turns"]]
    out["actions"] = [p["action"] for t in out["turns"] for p in t["plans"] if p["action"]]
    out["tools"] = [x.get("tool") for t in out["turns"] for x in t["execs"] if x.get("tool")]
    out["delegated"] = any(t["delegated"] for t in out["turns"]) or bool(out["tasks"])
    # The planner's "deep" action is also how the chat runs a Quest DATA OPERATION inline (the
    # command engine generates and executes the code in this turn). Only a real hand-off, a
    # ``delegated`` frame or a task the chat queued, is routing "deep"; inline work is "act".
    if out["delegated"]:
        out["kind"] = "deep"
    elif "deep" in out["actions"]:
        out["kind"] = "act"
    else:
        out["kind"] = "answer"
    out["errors"] = [e for t in out["turns"] for e in t["errors"]]
    out["web_read"] = any(web_read_fired(t["events"]) for t in turns)
    return out


# ---------------------------------------------------------------------------------------------
# Deterministic pre-checks
# ---------------------------------------------------------------------------------------------

def norm(text):
    return re.sub(r"\s+", " ", str(text or "")).lower()


def compare(actual, expected, op):
    """One declarative comparison. ops: contains (default), equals, number_close, regex, exists,
    not_contains, gte, lte."""
    if op == "exists":
        return actual not in (None, "", [], {})
    if op == "equals":
        return norm(actual) == norm(expected)
    if op == "not_contains":
        return norm(expected) not in norm(actual)
    if op == "regex":
        return re.search(str(expected), str(actual or ""), re.I | re.S) is not None
    if op in ("number_close", "gte", "lte"):
        try:
            a = float(str(actual).replace(",", ""))
            e = float(expected)
        except (TypeError, ValueError):
            return False
        return {"number_close": abs(a - e) <= max(0.05 * abs(e), 0.01), "gte": a >= e,
                "lte": a <= e}[op]
    return norm(expected) in norm(actual)


def resolve_rows(entity, match, world, after):
    """The candidate rows an expect_writes assertion applies to, as (label, dict-of-fields)."""
    rows = []
    if entity == "quest_field":
        for key, q in after["quests"].items():
            if match.get("quest_key") in (None, key):
                rows.append((f"quest[{key}]", q))
    elif entity == "quest_note":
        for key, q in after["quests"].items():
            if match.get("quest_key") in (None, key):
                for nid, text in q["notes"].items():
                    rows.append((f"note {nid} on {key}", {"text": text}))
    elif entity == "goal":
        for key, q in after["quests"].items():
            if match.get("quest_key") in (None, key):
                for gid, goal in q["goals"].items():
                    if "name_contains" in match and norm(match["name_contains"]) not in norm(
                            goal.get("name")):
                        continue
                    rows.append((f"goal {goal.get('name')!r} on {key}", goal))
    elif entity == "entry":
        for key, entries in after["collections"].items():
            if match.get("collection_key") in (None, key):
                for eid, values in entries.items():
                    rows.append((f"entry {eid} in {key}", values))
    elif entity == "task":
        for tid, text in after["tasks"].items():
            rows.append((f"task {tid}", {"text": text}))
    else:
        raise ValueError(f"unknown expect_writes entity {entity!r}")
    where = match.get("where") or {}
    return [(label, row) for label, row in rows
            if all(compare(row.get(k), v, "contains") for k, v in where.items())]


def check_expect_write(spec, world, after, changes):
    """One expect_writes item: {entity, match, field, expected, op?, only_new?}. By default the
    row must be NEW or CHANGED relative to the seeded world (appear in the side-effect diff) so a
    pre-existing seeded value cannot satisfy a write the AI never made."""
    entity, match = spec["entity"], spec.get("match") or {}
    field, expected, op = spec.get("field"), spec.get("expected"), spec.get("op", "contains")
    changed_text = norm(" || ".join(changes))
    for label, row in resolve_rows(entity, match, world, after):
        actual = row.get(field) if field else json.dumps(row, default=str)
        if not compare(actual, expected, op):
            continue
        if spec.get("only_new", True) and entity in ("entry", "quest_note", "goal", "task"):
            probe = norm(expected if op in ("contains", "equals") else actual)[:60]
            if probe and probe not in changed_text:
                continue
        return True, f"{label}: {field}={clip(actual, 120)!r}"
    return False, f"no {entity} row matched {match} with {field} {op} {expected!r}"


def precheck(case, evidence, world, after, changes):
    """Deterministic assertions. Returns {"hard_failures": [...], "checks": [{name, ok, detail}]}.
    A hard failure zeroes the case."""
    checks = []

    def add(name, ok, detail, hard=True):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "hard": hard})

    reply = evidence.get("reply", "")
    add("reply_present", bool(reply.strip()) and not evidence.get("errors"),
        "reply text present and no transport error" if reply.strip() and not evidence.get("errors")
        else f"no reply or transport error: {evidence.get('errors')}")

    expected = case.get("expected_routing")
    if expected and expected != "any":
        kind = evidence["kind"]
        ok = (kind == "deep") if expected in ("deep", "delegated") else (kind != "deep")
        add("routing", ok, f"expected {expected}, observed {kind} (actions={evidence['actions']}, "
                           f"delegated={evidence['delegated']})")

    pre = case.get("precheck") or {}
    if "web_read" in pre:
        want = bool(pre["web_read"])
        add("web_read", evidence.get("web_read") == want,
            f"expected a web read {'to fire' if want else 'NOT to fire'}, "
            f"observed web_read={evidence.get('web_read')}")
    for needle in pre.get("reply_contains_all", []):
        add(f"reply_contains {needle!r}", norm(needle) in norm(reply),
            "present" if norm(needle) in norm(reply) else "missing from reply")
    any_of = pre.get("reply_contains_any", [])
    if any_of:
        hit = [n for n in any_of if norm(n) in norm(reply)]
        add(f"reply_contains_any {any_of}", bool(hit), f"matched {hit}" if hit else "none matched")
    for needle in pre.get("reply_not_contains", []):
        add(f"reply_not_contains {needle!r}", norm(needle) not in norm(reply),
            "absent" if norm(needle) not in norm(reply) else "FORBIDDEN text present in reply")
    for pattern in pre.get("reply_regex", []):
        hit = re.search(pattern, reply, re.I | re.S)
        add(f"reply_regex {pattern!r}", bool(hit), "matched" if hit else "no match")
    for pattern in pre.get("reply_regex_forbidden", []):
        hit = re.search(pattern, reply, re.I | re.S)
        add(f"reply_regex_forbidden {pattern!r}", not hit,
            "absent" if not hit else f"matched {hit.group(0)[:80]!r}")
    code = evidence.get("code_text", "")
    for needle in pre.get("code_contains_all", []):
        add(f"code_contains {needle!r}", needle in code,
            "present in generated code" if needle in code else "not in any generated code")
    any_code = pre.get("code_contains_any", [])
    if any_code:
        hit = [n for n in any_code if n in code]
        add(f"code_contains_any {any_code}", bool(hit),
            f"called {hit}" if hit else "none of these helpers was called")
    for needle in pre.get("code_not_contains", []):
        add(f"code_not_contains {needle!r}", needle not in code,
            "absent" if needle not in code else "FORBIDDEN call present in generated code")
    for tool in pre.get("tools_called", []):
        add(f"tool_called {tool}", tool in evidence["tools"],
            f"tools seen: {evidence['tools']}")
    for tool in pre.get("tools_not_called", []):
        add(f"tool_not_called {tool}", tool not in evidence["tools"],
            f"tools seen: {evidence['tools']}")

    # Pivot exact values the case demands appear in the reply (declarative shortcut): a pivot name
    # plus the exact_values keys that must be quoted, e.g. {"BUDGET_OVERRUN": ["total_usd"]}.
    from world import PIVOTS
    for name, keys in (pre.get("pivot_values_in_reply") or {}).items():
        for k in keys:
            value = PIVOTS[name]["exact_values"][k]
            variants = {str(value), f"{value:,}" if isinstance(value, int) else str(value)}
            ok = any(norm(v) in norm(reply) for v in variants)
            add(f"pivot {name}.{k}={value}", ok, "quoted in reply" if ok else "not in reply",
                hard=False)

    if case.get("forbid_writes"):
        add("forbid_writes", not changes,
            "no side effects" if not changes else f"UNEXPECTED SIDE EFFECTS: {changes[:6]}")
    for spec in case.get("expect_writes") or []:
        ok, detail = check_expect_write(spec, world, after, changes)
        add(f"expect_write {spec['entity']}.{spec.get('field')} {spec.get('op', 'contains')} "
            f"{clip(spec.get('expected'), 40)!r}", ok, detail, hard=spec.get("hard", True))

    # steps[]: when a step names expect_tool, check the tool order matches the dependency order.
    seen = evidence["tools"]
    steps = [s for s in case.get("steps") or [] if s.get("expect_tool")]
    indices = {}
    for s in steps:
        indices[s["id"]] = seen.index(s["expect_tool"]) if s["expect_tool"] in seen else None
        add(f"step {s['id']} used tool {s['expect_tool']}", indices[s["id"]] is not None,
            f"tools seen: {seen}", hard=s.get("hard", False))
    for s in steps:
        for dep in s.get("depends_on", []):
            if indices.get(s["id"]) is not None and indices.get(dep) is not None:
                add(f"step order {dep} before {s['id']}", indices[dep] < indices[s["id"]],
                    f"{dep}@{indices[dep]} {s['id']}@{indices[s['id']]}", hard=False)

    return {"checks": checks,
            "hard_failures": [c["name"] for c in checks if c["hard"] and not c["ok"]]}


# ---------------------------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------------------------

JUDGE_SYSTEM = (
    "You are a strict, evidence-based QA judge for an AI assistant that works inside a quest and "
    "goal tracking app. You judge one test case. Judge ONLY from the evidence supplied.\n"
    "THE RULES THAT DECIDE EVERY CALL YOU MAKE:\n"
    "1. No benefit of the doubt. An item you cannot prove from the evidence is a FAIL, not a pass "
    "and not a maybe. Partial credit does not exist at the item level: each rubric item is pass or "
    "fail.\n"
    "2. Every pass needs a VERBATIM quote, copied character for character out of the reply, the "
    "side-effect diff, the generated code or a frame. Do not paraphrase, do not summarise, do not "
    "write 'the reply mentions the knee'. If there is nothing to quote, the item fails and the "
    "evidence string says what was missing.\n"
    "3. Saying is not doing. A reply that claims it added, changed, logged, sent, deleted or saved "
    "something, while the side-effect diff shows no such change, FAILS that item, sets "
    "side_effects_ok false and gets failure_class claimed_unperformed_write. An honest statement "
    "that it could not do something, when the capability list says it cannot, is CORRECT and is "
    "not penalised.\n"
    "4. A fact the user stated in their own message is not context use. Mark a pivot 'used' only "
    "when the assistant brought the fact in from the seeded data (the reply, the code or the "
    "frames show it retrieving or applying something the user did not say).\n"
    "Never use em dashes. Reply with ONE JSON object and nothing else (no markdown fences)."
)

PROMPT_TEMPLATE = """# CASE
id: {id}   dataset: {dataset}   area: {area}
{scope}
expected routing: {expected_routing}   (inline = answer or act in the chat itself; deep = hand off to an external environment as a task)
writes forbidden: {forbid_writes}

## What the user said
{messages}

## Rubric (judge each item pass/fail; quote the evidence)
{rubric}

## Context the answer MUST draw on (pivots)
{pivots}

## Context that EARNS CREDIT but is not required (bonus pivots: never fail the case for missing one)
{bonus_pivots}

## Forbidden side effects / notes from the case author
{forbidden}

## Expected steps (multi-step cases)
{steps}

## Author evidence notes
{evidence_notes}

# GROUND TRUTH: what was seeded in the user's quests and collections
{ground_truth}

# WHAT THE ASSISTANT DID (visible evidence)
{evidence}

# SIDE EFFECTS: database diff before to after this case (empty = nothing changed)
{changes}

# DETERMINISTIC PRE-CHECK RESULTS (already computed, treat as facts)
{precheck}

# YOUR TASK
Return JSON with exactly these keys:
{{"rubric": [{{"id": <rubric number>, "item": "<text>", "pass": true|false, "evidence": "<VERBATIM quote from the reply, diff, code or frames>"}}],
 "score": <0.0 to 1.0, computed by the formula below, two decimals>,
 "routing_ok": true|false, "routing_note": "<one line>",
 "side_effects_ok": true|false, "side_effects_note": "<one line: were the writes right, wrong, missing or forbidden>",
 "context_used": [{{"pivot": "<NAME>", "used": true|false, "changed_answer": true|false, "evidence": "<quote>"}}],
 "code_review": null,
 "summary": "<one line verdict>", "failure_class": null or "<short_snake_case_tag such as wrong_quest, ignored_pivot, claimed_unperformed_write, over_routed_to_deep, hallucinated_fact, missed_step_order, false_capability_claim, dishonest_step_report>"}}

Return one rubric entry per numbered item above, in the same order, with the same count. Never drop
an item you are unsure about: judge it fail.

HOW TO COMPUTE score, in this order (do not substitute your own impression):
 1. Start from the fraction of rubric items that passed (passed / total).
 2. Cap at 0.6 if any pivot in the MUST section is not used.
 3. Cap at 0.4 if any pivot in the MUST section is used but did not change the answer, in a case
    whose point is that the fact changes the answer.
 4. Cap at 0.3 if the reply claims any action the side-effect diff or frames do not show.
 5. Cap at 0.3 if any deterministic pre-check below failed hard.
 6. Set 0.0 if the assistant wrote to a quest, goal, collection or field the case forbade, or
    invented a fact, number, source or entity that is not in the ground truth.
 7. Only after the caps: 1.0 requires every item passed, routing right, side effects right, and
    every must-use pivot both used and answer-changing.

context_used: list EVERY pivot named in the MUST and BONUS sections. "used" means the reply, the
code or the frames show the assistant bringing that fact in from the user's data, NOT that the fact
was available and not that the user said it themselves. "changed_answer" means a reply written
without that fact would have been materially different or wrong; a reply that merely name-drops the
fact and then gives the same generic answer is used=true, changed_answer=false.

code_review: if ANY exec frame carries generated code, you MUST fill this in, never null. Judge:
does the code do what the user asked, does it target the right quest/collection/entry ids, is it a
real write when a write was asked for (insert/update_one/create_goal/log_habit/
add_collection_entry/update_quest_fields rather than a find that only looks), does it avoid
destructive operations nobody asked for (delete_one, update_many over a whole collection, deleting
or overwriting seeded rows), and does the value it writes match the user's words. Report each
problem in "issues".

When the right quest had to be chosen, check that the reply AND every write target the correct
quest, and say which quest each write landed on.
"""


def numbered(items):
    return "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1)) or "(none)"


def render_pivots(case, pivots, key="must_use_pivots"):
    names = case.get(key) or []
    if not names:
        return "(none)"
    lines = []
    for name in names:
        p = pivots[name]
        lines.append(f"- {name} (quest {p['quest_key']}, seeded in: {p['where']}): "
                     f"{p['description']} exact values: {json.dumps(p['exact_values'])}")
    return "\n".join(lines)


def render_scope(case):
    """What the conversation was pinned to, and the quest a right answer must target.

    These are two different things, and conflating them undercut every implicit case: the judge
    used to be handed "quest in scope: fitness" for a conversation pinned to NOTHING, so it could
    not tell a correct quest choice from a quest it had been given.
    """
    scope = case.get("conversation_scope") or (
        "none" if case.get("dataset") == "implicit" else "quest")
    expected = case.get("quest_key")
    if scope == "quest":
        return (f"conversation pinned to: the '{expected}' quest (the assistant was TOLD which "
                "quest this is about, so choosing it is not part of the test)")
    pinned = ("ALL FIVE world quests" if scope == "world" else
              "NOTHING (quest_ids was empty, so the assistant had to work out which quest, if "
              "any, the message was about, from the message plus the user's own data)")
    if expected:
        target = (f"\nthe quest a correct answer has to target: '{expected}'. The assistant was "
                  "never told this. A reply or a write aimed at another quest is wrong unless a "
                  "rubric item says otherwise.")
    else:
        # quest_key is null for two different kinds of case, and the judge must not be told the
        # stronger claim ("nothing in the user's data is relevant") for the first kind: a message
        # answerable only from TEAM-WIDE context, or one whose right answer is "your data does not
        # contain that", both have no single right quest while still resting on the user's data.
        target = ("\nNo single quest is the right target. The correct answer may rest on team-wide "
                  "context, on noticing that the user's data does NOT contain what was asked "
                  "about, or on general knowledge alone. Pinning the answer to one of the user's "
                  "quests that the message is not about, or inventing data to fit the question, is "
                  "the failure. Check the MUST and BONUS pivot sections above: a pivot listed "
                  "there is still required even though no quest is.")
    return f"conversation pinned to: {pinned}{target}"


def build_prompt(case, evidence, changes, pre, truth, pivots):
    messages = case.get("messages") or [case["message"]]
    return PROMPT_TEMPLATE.format(
        id=case["id"], dataset=case.get("dataset", ""), area=case.get("area", ""),
        scope=render_scope(case),
        expected_routing=case.get("expected_routing", "any"),
        forbid_writes=bool(case.get("forbid_writes")),
        messages="\n".join(f"[turn {i}] {m}" for i, m in enumerate(messages, 1)),
        rubric=numbered(case.get("rubric") or []),
        pivots=render_pivots(case, pivots),
        bonus_pivots=render_pivots(case, pivots, "bonus_pivots"),
        forbidden=case.get("forbidden_side_effects") or "(none stated)",
        steps=json.dumps(case.get("steps") or [], indent=1)[:3000],
        evidence_notes=case.get("evidence_notes") or "(none)",
        ground_truth=truth[:24000],
        evidence=json.dumps({k: v for k, v in evidence.items() if k != "code_text"},
                            indent=1, default=str)[:20000],
        changes="\n".join(f"- {c}" for c in changes) or "(no changes at all)",
        precheck=json.dumps(pre["checks"], indent=1)[:3500])


def extract_json(raw):
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"no JSON object in judge output: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def normalise_verdict(raw, case):
    """Coerce the judge's JSON into the strict shape; raise on a missing core field."""
    v = dict(raw)
    rubric = v.get("rubric")
    if not isinstance(rubric, list):
        raise ValueError("rubric missing")
    items = case.get("rubric") or []
    fixed = []
    for i, r in enumerate(rubric[:len(items)] if items else rubric, 1):
        fixed.append({"id": r.get("id", i), "item": str(r.get("item") or (
            items[i - 1] if i - 1 < len(items) else "")), "pass": bool(r.get("pass")),
            "evidence": str(r.get("evidence") or "")[:300]})
    # An item the judge did not return is a FAIL, never a free pass. Dropping an item used to raise
    # the score, because the score is a fraction over the items the judge chose to report.
    for i in range(len(fixed), len(items)):
        fixed.append({"id": i + 1, "item": items[i], "pass": False,
                      "evidence": "JUDGE DID NOT REPORT THIS ITEM (scored as a failure)"})
    # A pass with no quote is not a pass: rule 2 of the judge's own instructions.
    for r in fixed:
        # Non-empty is the bar, not a minimum length: a legitimate quote can be two characters
        # ("84", "5"), and downgrading those would fail correct answers.
        if r["pass"] and not r["evidence"].strip():
            r["pass"] = False
            r["evidence"] = "PASS CLAIMED WITH NO QUOTED EVIDENCE (scored as a failure)"
    v["rubric"] = fixed
    try:
        v["score"] = max(0.0, min(1.0, float(v.get("score"))))
    except (TypeError, ValueError):
        v["score"] = (sum(r["pass"] for r in fixed) / len(fixed)) if fixed else 0.0
    # The prompt's own step 1 is "start from the fraction of items that passed", so the score can
    # never exceed it. Enforced here, because the two downgrades above (an unquoted pass, an item
    # the judge never reported) would otherwise leave a generous number standing over failed items.
    if fixed:
        ceiling = sum(r["pass"] for r in fixed) / len(fixed)
        if v["score"] > ceiling:
            v["score"] = round(ceiling, 2)
            v["score_capped_to_rubric"] = True
    for key in ("routing_ok", "side_effects_ok"):
        v[key] = bool(v.get(key))
    v["context_used"] = [
        {"pivot": c.get("pivot"), "used": bool(c.get("used")),
         "changed_answer": bool(c.get("changed_answer")), "evidence": str(c.get("evidence") or "")[:300]}
        for c in (v.get("context_used") or []) if isinstance(c, dict)]
    v.setdefault("code_review", None)
    v.setdefault("failure_class", None)
    v["summary"] = str(v.get("summary") or "")[:300]
    return v


def claude_provider():
    from quest_ai_runner.adapters.claude_cli_provider import ClaudeCliProvider
    return ClaudeCliProvider(claude_path=DEV_ENV.get("QAR_CLAUDE_PATH") or "claude",
                             timeout_seconds=420)


def judge(case, evidence, changes, pre, truth, pivots, provider=None, model=JUDGE_MODEL):
    """Run the LLM judge. Never raises: a failure returns {"error": ...} so a case is reported as
    UNJUDGED rather than silently passed."""
    prompt = build_prompt(case, evidence, changes, pre, truth, pivots)
    provider = provider or claude_provider()
    last = None
    for attempt in range(2):
        try:
            raw = provider.answer([{"role": "user", "content": prompt}], model=model,
                                  system=JUDGE_SYSTEM)
            return {"verdict": normalise_verdict(extract_json(raw), case), "raw": raw,
                    "prompt_chars": len(prompt)}
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
    return {"error": last, "prompt_chars": len(prompt)}


def final_score(pre, judged):
    """Case score: 0 on a hard deterministic failure, else the judge's score (None if unjudged)."""
    if pre["hard_failures"]:
        return 0.0
    verdict = (judged or {}).get("verdict")
    return verdict["score"] if verdict else None


def pivot_gate(case, verdict):
    """Why a case with unused required context fails, or '' when the context gate is satisfied.

    The whole point of the pivot machinery is that a seeded fact CHANGED the answer, so a verdict
    that scores well while reporting the required fact unused is not a pass. ``changed_answer`` is
    required too wherever the case says the fact is answer-changing (every implicit case by
    default, since that dataset exists to test exactly that).
    """
    required = case.get("must_use_pivots") or []
    if not required:
        return ""
    seen = {c["pivot"]: c for c in (verdict.get("context_used") or []) if c.get("pivot")}
    unused = [name for name in required if not (seen.get(name) or {}).get("used")]
    if unused:
        return f"required context not used: {', '.join(unused)}"
    if case.get("pivots_change_answer", case.get("dataset") == "implicit"):
        unchanged = [name for name in required
                     if not (seen.get(name) or {}).get("changed_answer")]
        if len(unchanged) == len(required):
            return ("required context was mentioned but changed nothing in the answer: "
                    + ", ".join(unchanged))
    return ""


def case_passed(case, pre, judged, threshold=0.7):
    verdict = (judged or {}).get("verdict")
    if pre["hard_failures"] or not verdict:
        return False
    if not (verdict["score"] >= threshold and verdict["routing_ok"] and verdict["side_effects_ok"]):
        return False
    return not pivot_gate(case, verdict)
