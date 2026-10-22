"""Solution tags (the implementation of the solution tag-solutions): rule Q1 and the tag lookup.

Q1 fails a solution tag outside ``esoi/tags.csv``, a listed tag with no meaning, with no linked problem or with a linked
id that is not a problem record, and a tag used by no solution or by every solution; a tag on one solution is a note.
The lookup (``tags_of``, ``solutions_sharing``, and ``main`` as a command) reads a solution's tags as its tags column
plus its connected problems. ``esoi_rules.py`` (the plugin ``esoi.toml`` names) registers Q1 from here and re-exports
these names; ``evaluation/components/tag_solutions`` measures the rule on fixture tag lists.
"""
import csv
import sys
from pathlib import Path


TAG_PROCEDURE = "see docs/spec-selection.md, Solution tags: a tag links the problems it mitigates and is back-filled"


def split(cell) -> list:
    return [part.strip() for part in (cell or "").split(";") if part.strip()]


def tags_of(solution: dict) -> list:
    """A solution's tags as the lookup reads them: its ``tags`` column plus its connected problems (``problem_ids``),
    which count as tags too (the evolvable-structure skill, Solution, Tags) but are not sufficient on their own, so
    they are never copied into the column."""
    return list(dict.fromkeys(split(solution.get("tags")) + split(solution.get("problem_ids"))))


def solutions_sharing(wanted, solutions: list) -> list:
    """The ids of the solutions that carry any of ``wanted`` (tag ids or problem ids), in record order."""
    wanted = set(wanted)
    return [s.get("solution_id", "") for s in solutions if wanted & set(tags_of(s))]


def tag_problems(listed: list, solutions: list, problem_ids=None) -> list:
    """Q1 problem lines for the tag list ``listed`` (rows with tag, meaning, problem_ids) and the solutions' tags.

    What can be checked without judgement: every tag a solution uses is listed; every listed tag states its meaning
    and links at least one problem, and each linked problem exists (``problem_ids``, the ids of every problem record;
    None skips that check); every listed tag is used by some solution and not by all of them (a tag on every
    solution classifies nothing)."""
    known = {row.get("tag", "").strip(): row for row in listed if row.get("tag", "").strip()}
    users = {tag: [] for tag in known}
    bad = []
    for solution in solutions:
        sid = solution.get("solution_id", "")
        for tag in split(solution.get("tags")):
            if tag in users:
                users[tag].append(sid)
            else:
                bad.append(f"Q1 {sid}: tag `{tag}` is not in esoi/tags.csv ({TAG_PROCEDURE})")
    for tag, row in known.items():
        if not (row.get("meaning") or "").strip():
            bad.append(f"Q1 esoi/tags.csv {tag}: no meaning stated ({TAG_PROCEDURE})")
        linked = split(row.get("problem_ids"))
        if not linked:
            bad.append(f"Q1 esoi/tags.csv {tag}: links no problem, so it names no risk it mitigates ({TAG_PROCEDURE})")
        for pid in linked:
            if problem_ids is not None and pid not in problem_ids:
                bad.append(f"Q1 esoi/tags.csv {tag}: linked problem `{pid}` is not a problem record ({TAG_PROCEDURE})")
        count = len(users[tag])
        if count == 0:
            bad.append(f"Q1 esoi/tags.csv {tag}: used by no solution; retire it ({TAG_PROCEDURE})")
        elif solutions and count == len(solutions):
            bad.append(f"Q1 esoi/tags.csv {tag}: on every solution, so it identifies nothing; retire it ({TAG_PROCEDURE})")
    return bad


def tag_notes(listed: list, solutions: list) -> list:
    """Warnings, not failures: a tag on one solution only. It still lets the author of a second solution find the
    first (R1, R2), so it is allowed, but it is worth a back-fill check."""
    out = []
    for row in listed:
        tag = (row.get("tag") or "").strip()
        users = [s.get("solution_id", "") for s in solutions if tag in split(s.get("tags"))]
        if tag and len(users) == 1:
            out.append(f"note: Q1 esoi/tags.csv {tag}: used by one solution only ({users[0]}); check the back-fill "
                       f"({TAG_PROCEDURE})")
    return out


def read_tags(root: Path, structure_dir) -> list:
    path = Path(root) / structure_dir / "tags.csv"
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def solution_tags(ctx) -> list:
    """Rule Q1 over the records: ``ctx`` is the esoi rule context."""
    listed = read_tags(ctx.root, ctx.config.structure_dir)
    if not listed:
        return []
    solutions = ctx.every_solution()
    for line in tag_notes(listed, solutions):
        print(line, file=sys.stderr)
    known = {row.get("problem_id", "") for row in ctx.registry.problems}
    return tag_problems(listed, solutions, known)


def main(argv=None) -> int:
    """``python scripts/esoi_tags.py <tag, problem or solution id> ...``: the solutions that carry any of them, through
    their tags column or their connected problems. A solution id stands for its own tags."""
    import argparse
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("ids", nargs="+")
    args = ap.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    with (root / "esoi" / "solutions.csv").open(newline="") as handle:
        solutions = list(csv.DictReader(handle))
    by_id = {s.get("solution_id", ""): s for s in solutions}
    wanted = [t for i in args.ids for t in (tags_of(by_id[i]) if i in by_id else [i])]
    for sid in solutions_sharing(wanted, solutions):
        print(f"{sid}: {';'.join(t for t in tags_of(by_id[sid]) if t in wanted)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
