"""BASELINE: the first draft of a tag rule: a tag a solution uses must be in the list, and nothing else is checked. It
never asks whether a tag names the problems it mitigates, whether those problems exist, or whether a tag classifies
anything, and its lookup reads the tags column only, so a solution reached through its connected problems is missed.
Zero model calls."""
import csv
import io


def split(cell):
    return [p.strip() for p in (cell or "").split(";") if p.strip()]


def build(meter):  # noqa: ARG001
    def tags(kind, args):
        if kind == "lookup":
            wanted = set(args["ids"])
            return {"found": [s["solution_id"] for s in args["solutions"] if wanted & set(split(s.get("tags")))]}
        listed = {row["tag"] for row in csv.DictReader(io.StringIO(args["tags_csv"]))}
        return {"problems": [f"Q1 {s['solution_id']}: tag `{t}` is not in esoi/tags.csv" for s in args["solutions"]
                             for t in split(s.get("tags")) if t not in listed], "notes": []}
    return tags
