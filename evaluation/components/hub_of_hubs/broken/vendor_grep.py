"""BASELINE: the first draft anyone would write. Read the area name, grep the source for any vendor term, and call the area
registered, seamed and clean when a term is found. It has no dates, so nothing is ever fresh; it does not read the connections,
so nothing is by configuration; it knows no solution records, so nothing is selectable; it never tries a fake hub and cannot
tell a hub from a name in a comment. The floor the registry must beat. Zero model calls."""
import re

from hubofhubs import registry

from hub_of_hubs.binding import REPO, seats


def build(meter):  # noqa: ARG001
    terms = {s["area"]: registry.split(s["vendor_terms"]) for s in seats()}
    source = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in (REPO / "quest_ai_runner").rglob("*.py"))

    def measure(kind, args):
        named = any(re.search(re.escape(t), source, re.I) for t in terms.get(args["area"], []))   # an unbound area names no vendor
        return {"holds": named if kind in ("registered", "seam", "core_clean", "no_reinvented") else False}
    return measure
