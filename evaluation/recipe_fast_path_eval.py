"""RECIPE FAST PATH eval: does replaying a learned operation before context search beat the normal
plan -> search -> tool path, without ever firing on a request that only LOOKS like the operation?

Hermetic by design (no Quest backend, no deploy): five fake quest tools over an in-memory world,
a REAL model provider, a REAL context assembler over a small copy of this repo's docs. Two arms run
the identical sequence, only the recipe store differs:

  baseline : Orchestrator with no RecipeStore (today's path).
  recipes  : Orchestrator with a RecipeStore (the fast path).

For each tool the sequence first TEACHES it (a first-time request, planner path in BOTH arms; the
recipes arm learns a recipe from the success), then ASKS the scored requests:
  repeat    : the same operation again, paraphrased or with new values. Right tool, right arguments.
  near_miss : lexically close to a taught operation but a different intent (a read, a question).
              Right handling = the lookalike mutating tool is NOT called.
  unrelated : plain chat. No tool at all.

Scored per ask (verified against the fake world, never the reply text): correct, wrong-operation
(a forbidden tool ran), wall seconds, model tokens in/out, whether the recipe path answered.
Subsets are cumulative on purpose, run as 1 then 10 then 30; the full 30 is the whole dataset, so
there is nothing larger to run.

    python evaluation/recipe_fast_path_eval.py --subset 1|10|30 [--arm baseline|recipes|both]
"""
import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
env_file = REPO / ".env"
if env_file.is_file():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
sys.path.insert(0, str(REPO))

DATASET = json.loads((Path(__file__).parent / "recipe_fast_path_dataset.json").read_text())
SUBSET_10 = ["RH1", "RH2", "RN1", "RN2", "RT1", "RS1", "RG1", "NM1", "NM3", "U1"]
SUBSETS = {1: ["RH1"], 10: SUBSET_10, 30: [c["id"] for c in DATASET]}
TEACH_TOOLS = {"complete_habit": "mark my meditation habit done",
               "add_note": "add a note saying call the dentist on Friday",
               "log_time": "log 20 minutes on my running habit",
               "add_goal": "add a weekly goal called finish the quarterly report",
               "habit_streak": "what is my streak on the meditation habit"}


class World:
    def __init__(self):
        self.calls = []                     # (tool, args) in order, every invocation

    def tools(self):
        from quest_ai_runner.core.tools import ToolRegistry, ToolResult, ToolSpec

        def spec(name, desc, when, params, mutates, fn):
            def handler(args, ctx):
                self.calls.append((name, dict(args)))
                return ToolResult(ok=True, text=fn(args))
            props = {k: {"type": t} for k, t in params.items()}
            return ToolSpec(name=name, description=desc, handler=handler, when_to_use=when,
                            when_not_to_use="questions about the thing rather than doing it",
                            mutates=mutates, parameters={"type": "object", "required": list(params),
                                                         "properties": props})
        return ToolRegistry([
            spec("complete_habit", "Mark one habit as done for today.",
                 "The person says they did a habit and wants it checked off.",
                 {"habit": "string"}, True, lambda a: f"Marked the {a.get('habit')} habit done for today."),
            spec("add_note", "Save a short note on the quest.",
                 "The person asks to add or save a note.", {"text": "string"}, True,
                 lambda a: f"Saved note: {a.get('text')}"),
            spec("log_time", "Log minutes spent on a habit today.",
                 "The person says how long they spent on a habit.",
                 {"habit": "string", "minutes": "integer"}, True,
                 lambda a: f"Logged {a.get('minutes')} minutes on {a.get('habit')}."),
            spec("add_goal", "Create a goal for a period.",
                 "The person asks to add or create a goal.",
                 {"title": "string", "period": "string"}, True,
                 lambda a: f"Created {a.get('period')} goal: {a.get('title')}."),
            spec("habit_streak", "Read the current streak of a habit.",
                 "The person asks about the streak or status of a habit.",
                 {"habit": "string"}, False, lambda a: f"Your {a.get('habit')} streak is 6 days."),
        ])


def build(arm, work):
    corpus = work / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    for p in sorted((REPO / "docs").glob("*.md"))[:8]:
        shutil.copy(p, corpus / p.name)
    os.environ["QAR_CORPUS_ROOT"] = str(corpus)
    os.environ["QAR_CONVERSATION_SEARCH"] = "false"
    for drop in ("QAR_TOOLS_FILE", "QAR_CONFIG_FILE", "QAR_CONTEXT_PREAMBLE_FILE", "QAR_RECIPES"):
        os.environ.pop(drop, None)
    from quest_ai_runner.cli import _config_from_env
    from quest_ai_runner.config import build_orchestrator
    from quest_ai_runner.core.recipes import RecipeStore
    cfg = _config_from_env()
    cfg.deep_runner = None                       # nothing in this eval may start a deep run
    world = World()
    cfg.tool_registry = world.tools()
    if arm == "recipes":
        cfg.recipe_store = RecipeStore(work / "recipes.json")
    orch = build_orchestrator(cfg)
    assert not getattr(orch, "deep_runner", None)
    return orch, world


def run_turn(orch, world, message, quest_id="q_eval"):
    world.calls.clear()
    started = time.monotonic()
    err = None
    res = None
    try:
        res = orch.run(message, quest_id=quest_id)
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    seconds = time.monotonic() - started
    return res, list(world.calls), seconds, err


def verify(case, calls):
    """(correct, wrong_operation, note) from the world's recorded calls, never the reply."""
    names = [c[0] for c in calls]
    forbid = case.get("forbid_tool")
    wrong = bool(forbid and ((forbid == "any" and names) or (forbid != "any" and forbid in names)))
    exp = case.get("expect_tool")
    if exp is None:
        return (not names, wrong, f"called {names}" if names else "no tool")
    hits = [c for c in calls if c[0] == exp]
    if len(hits) != 1:
        return False, wrong, f"expected one {exp}, got {names}"
    got = hits[0][1]
    for k, want in (case.get("expect_args") or {}).items():
        have = got.get(k)
        if isinstance(want, int):
            try:
                if int(have) != want:
                    return False, wrong, f"{k}={have!r}, wanted {want}"
            except (TypeError, ValueError):
                return False, wrong, f"{k}={have!r}, wanted {want}"
        elif str(want).lower() not in str(have).lower():
            return False, wrong, f"{k}={have!r}, wanted ~{want!r}"
    other_mut = [n for n in names if n != exp and n != "habit_streak"]
    if other_mut:
        return False, wrong, f"extra mutating call {other_mut}"
    return True, wrong, "ok"


def run_arm(arm, subset):
    ids = set(SUBSETS[subset])
    cases = [c for c in DATASET if c["id"] in ids]
    work = Path(tempfile.mkdtemp(prefix=f"recipeeval_{arm}_"))
    orch, world = build(arm, work)
    # Warm the context store once so no scored turn pays the cold bootstrap.
    run_turn(orch, world, "hello, what can you do?")
    teach_for = []
    for c in cases:
        t = c.get("tool")
        if t and t in TEACH_TOOLS and t not in teach_for:
            teach_for.append(t)
    teach_rows = []
    for t in teach_for:
        res, calls, secs, err = run_turn(orch, world, TEACH_TOOLS[t])
        teach_rows.append(dict(tool=t, seconds=round(secs, 1), calls=[c[0] for c in calls], error=err))
        print(f"[{arm}] teach {t}: {calls} {secs:.1f}s {err or ''}", flush=True)
    rows = []
    for c in cases:
        res, calls, secs, err = run_turn(orch, world, c["ask"])
        correct, wrong, note = verify(c, calls) if not err else (False, False, err)
        row = dict(id=c["id"], group=c["group"], ask=c["ask"], correct=correct, wrong_op=wrong,
                   note=note, seconds=round(secs, 2),
                   tokens_in=getattr(res, "tokens_in", 0), tokens_out=getattr(res, "tokens_out", 0),
                   recipe=bool(res is not None and res.exit_reason == "recipe"),
                   exit_reason=getattr(res, "exit_reason", None), steps=getattr(res, "steps", None))
        rows.append(row)
        print(f"[{arm}] {c['id']:4} {'OK ' if correct else 'BAD'} wrong={wrong} recipe={row['recipe']} "
              f"{secs:6.1f}s tok={row['tokens_in']}+{row['tokens_out']} {note}", flush=True)
    return dict(arm=arm, subset=subset, teach=teach_rows, rows=rows,
                recipes_learned=(len(orch.recipes.all()) if getattr(orch, "recipes", None) else 0))


def summarize(result):
    rows = result["rows"]
    n = len(rows)
    out = dict(arm=result["arm"], subset=result["subset"], n=n,
               correct=sum(r["correct"] for r in rows), wrong_op=sum(r["wrong_op"] for r in rows),
               recipe_answered=sum(r["recipe"] for r in rows),
               median_seconds=round(statistics.median(r["seconds"] for r in rows), 2),
               mean_seconds=round(statistics.mean(r["seconds"] for r in rows), 2),
               tokens_in_total=sum(r["tokens_in"] for r in rows),
               tokens_out_total=sum(r["tokens_out"] for r in rows),
               recipes_learned=result["recipes_learned"])
    for g in sorted({r["group"] for r in rows}):
        gr = [r for r in rows if r["group"] == g]
        out[f"correct_{g}"] = f"{sum(r['correct'] for r in gr)}/{len(gr)}"
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subset", type=int, choices=sorted(SUBSETS), required=True)
    ap.add_argument("--arm", choices=["baseline", "recipes", "both"], default="both")
    ap.add_argument("--out", default=None, help="directory for the JSON result")
    a = ap.parse_args()
    arms = ["baseline", "recipes"] if a.arm == "both" else [a.arm]
    out_dir = Path(a.out or os.environ.get("QAR_EVAL_OUT_DIR") or tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for arm in arms:
        result = run_arm(arm, a.subset)
        (out_dir / f"recipe_eval_{arm}_{a.subset}.json").write_text(json.dumps(result, indent=1))
        s = summarize(result)
        summaries.append(s)
        print(json.dumps(s), flush=True)
    return summaries


if __name__ == "__main__":
    main()
