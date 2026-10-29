"""Reads the hub registry and the code, and computes the hub-of-hubs numbers. Pure functions over files: no model call, no
network. Shared by the implementation, the documentation renderer and the tests, so one definition serves all three."""
import csv
import os
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ESOI = REPO / "esoi"
STALENESS_DAYS = 90          # the bar: older research is stale (the repo's metric-run-policy idiom, a quarter)
REGISTERED = ("builds_on", "compared", "no_hub_exists")
CONFIG_SHARE_BAR = 0.5       # an area counts as "by config" when at least this share of its capabilities need no code


def read(name):
    with open(ESOI / name, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def today():
    """Today, or the day ``QAR_HUBS_TODAY`` names (an ISO date) so a test can age the registry."""
    return date.fromisoformat(os.environ.get("QAR_HUBS_TODAY") or date.today().isoformat())


def days_since(iso):
    try:
        return (today() - date.fromisoformat(iso)).days
    except (TypeError, ValueError):
        return None


def parse_hubs(cell):
    """``name|url|license`` entries joined by semicolons -> list of (name, url, license)."""
    out = []
    for entry in (cell or "").split(";"):
        if entry.strip():
            parts = (entry.split("|") + ["", ""])[:3]
            out.append(tuple(p.strip() for p in parts))
    return out


def split(cell):
    return [x.strip() for x in (cell or "").split(";") if x.strip()]


def capabilities_of(area, capabilities):
    return [c for c in capabilities if c["area"] == area]


def solution_facts(solution_id):
    """(authoritative metric id, implementation count) for a solution, from the records."""
    solutions = {r["solution_id"]: r for r in read("solutions.csv")}
    count = sum(1 for r in read("implementations.csv") if r["solution_id"] == solution_id)
    row = solutions.get(solution_id)
    return (row or {}).get("authoritative_metric_id", ""), count, row is not None


def declared_names(solution_id, names_of=None):
    """The implementation names the code declares for a solution (the live registry, ``core/resolve.py``)."""
    if names_of is None:
        from quest_ai_runner.core import resolve
        names_of = resolve.names
    try:
        return names_of(solution_id)
    except ValueError:
        return []


def area_facts(row, findings, capabilities, vendor_branches, names_of=None):
    """Every per-area fact the cases and the documentation read."""
    days = days_since(row["last_researched"])
    caps = capabilities_of(row["area"], capabilities)
    config = [c for c in caps if c["connect"] == "config"]
    metric, implementations, known = solution_facts(row["seam_solution_id"]) if row["seam_solution_id"] else ("", 0, False)
    names = declared_names(row["seam_solution_id"], names_of) if row["seam_solution_id"] else []
    recorded = any(f["finding_id"] == row["finding_id"] for f in findings)
    return dict(
        area=row["area"], days=days, status=row["status"], capabilities=len(caps), by_config=len(config),
        registered=row["status"] in REGISTERED and recorded,
        fresh=days is not None and days <= STALENESS_DAYS,
        config_ok=bool(caps) and len(config) / len(caps) >= CONFIG_SHARE_BAR,
        seam=bool(names), declared=len(names),
        selectable=bool(metric) and implementations >= 2 and known,
        vendor_branches=vendor_branches.get(row["area"], []),
    )


def vendor_branches_by_area(hubs):
    from hub_of_hubs import scan
    return scan.scan_core({h["area"]: split(h["vendor_terms"]) for h in hubs})


def all_facts(names_of=None):
    hubs, findings, caps = read("hubs.csv"), read("hub_findings.csv"), read("hub_capabilities.csv")
    branches = vendor_branches_by_area(hubs)
    return [area_facts(h, findings, caps, branches, names_of) for h in hubs], caps


def metrics(facts, caps, fake_hubs_pass=None):
    """The numbers: coverage, integration-by-config share, freshness (max, mean days), the code-structure shares, score."""
    n = len(facts) or 1
    days = [f["days"] for f in facts if f["days"] is not None]
    coverage = sum(1 for f in facts if f["registered"]) / n
    config_share = (sum(1 for c in caps if c["connect"] == "config") / len(caps)) if caps else 0.0
    fresh_share = sum(1 for f in facts if f["fresh"]) / n
    seam_share = sum(1 for f in facts if f["seam"]) / n
    selectable_share = sum(1 for f in facts if f["selectable"]) / n
    clean_share = sum(1 for f in facts if not f["vendor_branches"]) / n
    code = (seam_share + selectable_share + clean_share) / 3
    out = dict(coverage=coverage, config_share=config_share, fresh_share=fresh_share,
               days_max=max(days) if days else None, days_mean=(sum(days) / len(days)) if days else None,
               seam_share=seam_share, selectable_share=selectable_share, clean_share=clean_share,
               vendor_branches=sum(len(f["vendor_branches"]) for f in facts), code=code)
    out["score"] = (coverage + config_share + fresh_share + code) / 4
    return out
