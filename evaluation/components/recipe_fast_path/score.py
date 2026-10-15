"""Score one recipe-fast-path case from the world's recorded tool calls (never the reply text). Ported from
``evaluation/recipe_fast_path_eval.py``: ``correct`` = the expected single tool call with the expected
argument values and no extra mutating call (or no call at all when ``expect_tool`` is null);
``wrong_op`` = a forbidden lookalike tool ran. ``ok`` = correct and not wrong_op.

The TEACH message for a case's tool is sent once per run (per solution instance) before its first ask, in
the same world, with the log cleared afterwards. Its model calls land in that case's call count."""
from recipe_fast_path.fake_world import TEACH_TOOLS, RecipeWorld

WORLD = RecipeWorld()
_taught = {}   # id(fn) -> set of tools already taught to that solution instance


def verify(case, calls):
    """(correct, wrong_operation, note) from the recorded calls."""
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


def score_case(fn, case):
    taught = _taught.setdefault(id(fn), set())
    tool = case.get("tool")
    if tool in TEACH_TOOLS and tool not in taught:
        taught.add(tool)
        WORLD.reset()
        fn(TEACH_TOOLS[tool], WORLD)
    WORLD.reset()
    out = fn(case["ask"], WORLD)
    correct, wrong, note = verify(case, list(WORLD.calls))
    return dict(ok=correct and not wrong, correct=correct, wrong_op=wrong, group=case["group"],
                recipe=out.get("exit_reason") == "recipe", got=",".join(n for n, _ in WORLD.calls) or "(no tool)",
                note=note)


def aggregate(rows):
    parts = []
    for g in sorted({r.get("group") for r in rows if r.get("group")}):
        gr = [r for r in rows if r.get("group") == g]
        parts.append(f"{g} {sum(bool(r.get('correct')) for r in gr)}/{len(gr)}")
    return (f"correct {sum(bool(r.get('correct')) for r in rows)}/{len(rows)} ({', '.join(parts)}), "
            f"wrong operations {sum(bool(r.get('wrong_op')) for r in rows)}, "
            f"answered by a replayed recipe {sum(bool(r.get('recipe')) for r in rows)}")
