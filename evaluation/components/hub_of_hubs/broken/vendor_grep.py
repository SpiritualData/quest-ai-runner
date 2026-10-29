"""BASELINE: the first draft anyone would write. Read the area name, grep the source for any vendor term, and call the area
registered, seamed and clean when a term is found. It has no dates, so nothing is ever fresh; it does not read the
capabilities, so nothing is by configuration; it knows no solution records, so nothing is selectable; and it never tries a
fake hub. It cannot tell a hub from a name in a comment. The floor the registry must beat. Zero model calls."""
import re

from hub_of_hubs import registry


def build(meter):  # noqa: ARG001
    hubs = {h["area"]: h for h in registry.read("hubs.csv")}
    source = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                       for p in (registry.REPO / "quest_ai_runner").rglob("*.py"))

    def measure(kind, args):
        hub = hubs[args["area"]]
        named = any(re.search(re.escape(t), source, re.I) for t in registry.split(hub["vendor_terms"]))
        return {"holds": named if kind in ("registered", "seam", "core_clean") else False}
    return measure
