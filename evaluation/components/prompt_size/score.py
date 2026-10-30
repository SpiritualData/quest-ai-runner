"""Score one prompt_size case: build the fixed inputs, assemble the prompt, measure it.

A case passes when the prompt is within the assembly point's DERIVED budget (``budgets.py``: the cheapest tier's
context window, a stated share of it, and the cost per call; no number here is fitted to a measurement) AND holds
zero duplicated blocks. Every row also reports its headroom, the share of the budget left unused. A duplicated block is either a second (or later) context-updates block, or an
identical paragraph of at least ``MIN_BLOCK_CHARS`` characters repeated (whitespace-normalised). The
2026-10-06 incident had 16 duplicated context-update blocks in an 838K-character autopilot prompt.
"""
import json
import re
from collections import Counter
from pathlib import Path

from prompt_size import budgets
from quest_ai_runner.core import prompt_budget

MIN_BLOCK_CHARS = 200


def duplicate_blocks(text):
    """``(extra_update_blocks, extra_identical_paragraphs)`` in ``text``."""
    from quest_ai_runner.core.prompt_budget import count_blocks
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text or "")]
    seen = Counter(p for p in paras if len(p) >= MIN_BLOCK_CHARS)
    return max(0, count_blocks(text) - 1), sum(n - 1 for n in seen.values() if n > 1)


def score_sweep(fn, case):
    """A production case runs ``sweep`` samples from a light day to the heaviest; the ceiling must hold for all."""
    from prompt_size import fixtures
    sweep = case["sweep"]
    rows = [score_case(fn, dict(case, sweep=sweep, sample=i, sweep_run=True)) for i in range(sweep)]
    sizes = [r["chars"] for r in rows]
    dups = sum(r["dups"] for r in rows)
    budget = budgets.derive(case["point"])["budget_chars"]
    ok = max(sizes) <= budget and dups == 0
    note = (f"{sweep} production samples: median {sorted(sizes)[len(sizes) // 2]}, p95 {p95(sizes)}, max {max(sizes)} "
            f"chars (budget {budget}), {dups} duplicated blocks")
    return dict(ok=ok, chars=max(sizes), p95_chars=p95(sizes), dups=dups, point=case["point"], budget=budget,
                headroom=round(1 - max(sizes) / budget, 3), got=f"max {max(sizes)} chars", note=note)


# What each expected route of the classification cases depends on in the planner doctrine: the ids of PLANNER_RULES whose
# text the planner needs to take that route (a route the tools block describes needs only the read grammar).
ROUTE_RULES = {"answer": ("answer-action", "read-real-content", "command-vs-question"), "tool": ("read-action",),
               "deep": ("command-vs-question", "code-file-commands", "execute-dont-describe", "can-it-be-done",
                        "deep-action", "deep-difficulty", "reach-of-a-read"),
               "confirm": ("confirm-action", "command-vs-question"), "clarify": ("command-vs-question",)}


def marker(rule):
    """The first line of a rule's text that carries no format slot: how the rule is found in a rendered prompt ("" when
    the rule is only a slot, which no text can show)."""
    return next((line.strip() for line in rule.text.splitlines() if line.strip() and "{" not in line), "")


def labelled_situation(row):
    """The turn facts a labelled classification case stands for, from its expected routes (no model, no words)."""
    routes = set(row["expect"])
    asks = True if routes & {"tool", "deep"} and "answer" not in routes else False if routes == {"answer"} else None
    return dict(mode="normal", deep_available=True, tools_offered=bool(row.get("tools")), run_in_progress=False,
                has_material=True, asks_change=asks, reach="outside" if routes == {"deep"} else "inside")


def situation_rows(case):
    """(name, message, situation, rule ids the turn needs, rule ids that must be left out) for a situation case."""
    if case.get("labelled_cases"):
        rows = json.loads((Path(__file__).resolve().parents[1] / case["labelled_cases"] / "cases.json").read_text())
        return [(r["id"], r["message"], labelled_situation(r),
                 {"decision-order", *(i for route in r["expect"] for i in ROUTE_RULES[route])}, set()) for r in rows]
    return [(case["name"], "x", case["situation"], set(case["requires"]) | {"decision-order"}, set(case.get("absent", [])))]


def score_situations(fn, case):
    """Per turn: every rule the turn needs is in the prompt the model would read (that is the pass). The rules a turn
    could do without are reported, not required, so the full profile passes too and the saving is read off the tokens:
    the size is compared with the full profile on the same turn (the budget is reported, not enforced: the planner
    point's own cases enforce it)."""
    from quest_ai_runner.core.planner_prompt import PLANNER_RULES
    markers = {r.id: marker(r) for r in PLANNER_RULES}
    budget = budgets.derive("planner")["budget_chars"]
    problems, sizes, fulls, kept = [], [], [], []
    for name, message, situation, needs, absent in situation_rows(case):
        inputs = dict(user_message=message, transcript="", context_view="context", gathered=[], situation=situation)
        text, full = fn("planner_situation", inputs), fn("planner_situation", dict(inputs, profile="full"))
        sizes.append(len(text))
        fulls.append(len(full))
        problems += [f"{name}: rule {i} missing" for i in sorted(needs) if markers[i] not in text]
        kept += [i for i in sorted(absent) if markers[i] and markers[i] in text]
    tokens, full_tokens = (round(sum(v) / len(v) / prompt_budget.CHARS_PER_TOKEN) for v in (sizes, fulls))
    note = (f"{len(sizes)} turn(s): mean {tokens} tokens against {full_tokens} for the full profile "
            f"({1 - tokens / full_tokens:.0%} fewer; range {round(min(sizes) / prompt_budget.CHARS_PER_TOKEN)} to "
            f"{round(max(sizes) / prompt_budget.CHARS_PER_TOKEN)}); " + ("; ".join(problems[:6]) or "every needed rule present")
            + (f"; {len(kept)} rule(s) kept that the turn could do without" if kept else ""))
    return dict(ok=not problems, chars=max(sizes), dups=0, point=case["point"], budget=budget, tokens=tokens,
                full_tokens=full_tokens, headroom=round(1 - max(sizes) / budget, 3), got=f"{tokens} tokens", note=note)


def score_case(fn, case):
    from prompt_size import fixtures
    if case["point"] == "planner_situation":
        return score_situations(fn, case)
    if case.get("sweep") and not case.get("sweep_run"):
        return score_sweep(fn, case)
    text = fn(case["point"], fixtures.build_inputs(case))
    chars = len(text)
    blocks, paras = duplicate_blocks(text)
    dups = blocks + paras
    derived = budgets.derive(case["point"])
    budget = derived["budget_chars"]
    ok = chars <= budget and dups == 0
    note = (f"{chars} chars (budget {budget}, {derived['basis']}), {dups} duplicated blocks "
            f"({blocks} context-update, {paras} identical paragraphs)")
    return dict(ok=ok, chars=chars, dups=dups, point=case["point"], budget=budget,
                headroom=round(1 - chars / budget, 3), got=f"{chars} chars", note=note)


def p95(values):
    """Nearest-rank 95th percentile of ``values`` (the max when there are fewer than 20)."""
    vals = sorted(values)
    if not vals:
        return 0
    return vals[min(len(vals) - 1, max(0, -(-95 * len(vals) // 100) - 1))]


def aggregate(rows):
    """Per assembly point: the derived budget, the max and p95 of the fixed fixtures, and of the production sweep on
    its own line, with the smallest headroom (share of the budget unused) of the point."""
    by = {}
    for r in rows:
        key = f"{r.get('point')}{' (production sweep)' if 'p95_chars' in r else ''}"
        by.setdefault(key, []).append(r)
    parts = []
    for p, rs in sorted(by.items()):
        sizes = [r.get("chars", 0) for r in rs]
        p95s = [r["p95_chars"] for r in rs if "p95_chars" in r] or [p95(sizes)]
        parts.append(f"{p}: max {max(sizes)} chars of budget {rs[0].get('budget')}, p95 {max(p95s)} chars, "
                     f"headroom {min(r.get('headroom', 0) for r in rs):.0%}, {sum(r.get('dups', 0) for r in rs)} dups")
    return "; ".join(parts)
