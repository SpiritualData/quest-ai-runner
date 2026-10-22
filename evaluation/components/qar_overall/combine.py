"""The combined QAR metric (``metric-qar-overall``, the authoritative metric of QS1), computed from recorded rows only.

Nothing here runs an arm. Each sub-metric (QME1 to QME7, one case each in ``cases.json``) names the recorded arms that
measure it in two tiers:

  cheap  offline arms (free, whole set) or a small sampled run of a paid arm (``--sample N --max-calls M``): the everyday
         number, the QUICK score
  full   the whole input set, against a live model where the arm is paid: run only by decision with a budget, the FULL
         score. An arm not built yet is listed ``planned`` with its planned tiers and cost estimate, never faked.

Each arm is normalised to 0..1 by its sub-metric's ``rule`` (``RULES``) from its latest recorded row in that tier, and
counts only when that row is current (``current``): a row with no commit, recorded on uncommitted code, on other cases, or
older than a committed change to the measured code is stale, and a stale or missing arm is unmeasured. Humility: an
unmeasured arm is never a pass. Every score is reported three ways, the floor first (unmeasured counted 0) beside the
measured share of the weight, then the headline (measured weight only, renormalised) and the ceiling (unmeasured counted 1).

Weights: each of QS1's ideals carries a weight in ``esoi.toml`` ``[balance.metric-qar-overall]`` (with a written reason),
split equally over the sub-metrics that serve it (their ``ideal_ids`` in ``esoi/metrics.csv``). An ideal no sub-metric
serves keeps its weight unmeasured. QME6 and QME7 are read from the structure itself (``structure_*`` rules): the share of
solutions whose selected arm has a current row that no rival beats.
"""
import datetime
import json
import subprocess
from functools import lru_cache
from pathlib import Path

import common
from esoi import leaderboard
from esoi.harness import load_cases, own_code_paths
from esoi.registry import first_arm, load as load_registry
from esoi.tables import ids

CONFIG = common.CONFIG
WEIGHTS_KEY = "metric-qar-overall"   # keyed by the metric, not the component: the harness's accuracy/cost/time balance
SELF = "QS1"                          # is read by component name, and this table is not that balance
TIERS = ("cheap", "full")


# ---------------------------------------------------------------- settings and weights

def settings(config=CONFIG):
    table = config.balance.get(WEIGHTS_KEY) or {}
    ideals = {k: float(v) for k, v in (table.get("ideals") or {}).items()}
    if not ideals or abs(sum(ideals.values()) - 1.0) > 1e-6:
        raise ValueError(f"[balance.{WEIGHTS_KEY}] ideals must be weights summing to 1, got {ideals}")
    return {"ideals": ideals, "reasons": dict(table.get("reasons") or {}), "bar": float(table.get("bar", 0.8)),
            "cost_per_passing_turn_usd": float(table.get("cost_per_passing_turn_usd", 0.05))}


def metric_rows(config=CONFIG):
    return {m["metric_id"]: m for m in load_registry(config).metrics}


def submetric_weights(sub_ids, config=CONFIG):
    """``({sub-metric: weight}, {ideal: [sub-metrics]}, unserved weight)``: each ideal's weight split equally over the
    sub-metrics whose ``ideal_ids`` name it."""
    cfg, metrics = settings(config), metric_rows(config)
    serving = {i: [m for m in sub_ids if i in ids(metrics.get(m, {}).get("ideal_ids", ""))] for i in cfg["ideals"]}
    weights = {m: 0.0 for m in sub_ids}
    for ideal, weight in cfg["ideals"].items():
        for m in serving[ideal]:
            weights[m] += weight / len(serving[ideal])
    unserved = sum(w for i, w in cfg["ideals"].items() if not serving[i])
    return weights, serving, unserved


# ---------------------------------------------------------------- rows, freshness and normalisation

def history():
    return leaderboard.all_results(CONFIG.results_path)


def git(*args):
    done = subprocess.run(["git", *args], cwd=CONFIG.root, capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else None


def head():
    return git("rev-parse", "--short", "HEAD") or ""


@lru_cache(maxsize=None)
def committed_changes(sha):
    """Measured files changed by commits since ``sha`` (the working tree is not compared: rows are recorded from committed
    code, and other people's uncommitted edits are not what any row measured). None when the commit is unknown here."""
    out = git("diff", "--name-only", sha, "HEAD", "--", *own_code_paths(CONFIG), CONFIG.evaluation_dir,
              f":(exclude){CONFIG.results}")
    return None if out is None else out.split()


@lru_cache(maxsize=None)
def cases_now(component):
    return leaderboard.cases_digest(component, CONFIG.evaluation)


@lru_cache(maxsize=None)
def whole_set(component):
    try:
        return len(load_cases(component, CONFIG))
    except Exception:  # noqa: BLE001  a component that is not built has no set
        return 0


def current(component, row):
    """Why ``row`` does not count, or "" when it is current (the rule of ``esoi check --strict``, against HEAD)."""
    if row is None:
        return "no recorded row"
    sha = (row.get("git_sha") or "").strip()
    if not sha:
        return "the row has no git_sha"
    if sha.endswith("-dirty"):
        return "recorded on uncommitted code"
    if (row.get("cases_sha") or "") != cases_now(component):
        return "recorded on other cases"
    changed = committed_changes(sha)
    if changed is None:
        return f"its commit {sha} is not known here"
    if changed:
        return f"stale, code changed since {sha}"
    return ""


def num(row, key):
    try:
        return float(row.get(key) or 0)
    except ValueError:
        return 0.0


def cost_known(row):
    return str(row.get("cost_usd", "")).strip() != ""


def pass_rate(row):
    return num(row, "passed") / max(num(row, "cases"), 1.0)


def pass_per_dollar(row):
    """The pass rate, scaled down when a passing turn costs more than the budget per passing turn: ``pass rate x min(1,
    budget / (cost / passed))``. A run that passed nothing scores 0; an unknown price is never read as free."""
    passed = num(row, "passed")
    if passed <= 0:
        return 0.0
    if not cost_known(row):
        return 0.0
    cost = num(row, "cost_usd")
    budget = settings()["cost_per_passing_turn_usd"]
    return pass_rate(row) * (1.0 if cost <= 0 else min(1.0, budget / (cost / passed)))


RULES = {
    "pass_rate": ("passed cases / cases run", pass_rate),
    "pass_per_dollar": ("pass rate x min(1, budget per passing turn / dollars per passing turn)", pass_per_dollar),
    "structure_coverage": ("solutions (all) whose selected arm has a current row no rival beats / all solutions", None),
    "structure_selection": ("measured solutions whose selected arm has a current row no rival beats / measured solutions", None),
}


def pick_row(arm_history, tier, component):
    """The row an arm's tier reads: cheap, the latest row of any size; full, the latest row on the whole set."""
    if not arm_history:
        return None
    if tier == "cheap":
        return arm_history[-1]
    whole = whole_set(component)
    full = [r for r in arm_history if whole and num(r, "cases") >= whole]
    return full[-1] if full else None


def cost_per_case(arm_history):
    """Dollars per case of the latest recorded paid row of an arm, or None when it never recorded a priced paid run."""
    paid = [r for r in arm_history or [] if num(r, "model_calls") > 0 and cost_known(r)]
    if not paid:
        return None
    return num(paid[-1], "cost_usd") / max(num(paid[-1], "cases"), 1.0)


def estimate(per_case, cases):
    return None if per_case is None else round(per_case * cases, 2)


# ---------------------------------------------------------------- one sub-metric

def arm_reading(spec, tier, rule, log, choose):
    """One arm in one tier: the row ``choose`` picks, whether it counts, and what running its tier would cost (per-case
    cost of its latest paid row times the tier's size: the sample for cheap, the whole set for full; offline arms cost 0)."""
    run = (spec.get("run") or {}).get(tier, "")
    if spec.get("planned"):
        source = spec.get("estimate_from")
        per_case = cost_per_case(log.get(source.split("/")[0], {}).get(source.split("/")[1])) if source else None
        size = spec.get("planned_cases", 0) if tier == "full" else spec.get("cheap_cases", 0)
        return dict(arm=spec["arm"], tier=tier, share=spec["share"], planned=True, current=False, why="not built yet",
                    value=None, estimate=estimate(per_case, size), run=run, what=spec.get("what", ""), paid=True)
    comp, impl = spec["arm"].split("/")
    arm_history = log.get(comp, {}).get(impl, [])
    row, why = choose(comp, arm_history, tier)
    fn = RULES[rule][1]
    value = fn(row) if row is not None and fn else None
    paid = bool(spec.get("paid"))
    size = whole_set(comp) if tier == "full" else spec.get("cheap_cases", whole_set(comp))
    return dict(arm=spec["arm"], tier=tier, share=spec["share"], planned=False, current=not why, why=why,
                value=round(value, 4) if value is not None else None,
                date=(row or {}).get("date", ""), sha=(row or {}).get("git_sha", ""),
                cases=int(num(row, "cases")) if row else 0, passed=int(num(row, "passed")) if row else 0,
                whole=whole_set(comp), cost=(row or {}).get("cost_usd", ""), model=(row or {}).get("model", ""),
                paid=paid, estimate=estimate(cost_per_case(arm_history), size) if paid else 0.0,
                run=run, proxy=spec.get("proxy", ""))


def honest_choice(comp, arm_history, tier):
    row = pick_row(arm_history, tier, comp)
    return row, current(comp, row)


def structure_reading(rule, choose_current=current):
    """QME6 and QME7, read from the records and the results log: per measured solution, its selected arm's latest row is
    current and no rival beats it. QS1 itself is left out (its own metric would count itself)."""
    reg = load_registry(CONFIG)
    log = history()
    latest = {c: {i: h[-1] for i, h in impls.items()} for c, impls in log.items()}
    measured = [r for r in reg.solutions if r["solution_id"] != SELF]
    specs = [r for r in reg.specifications if r["solution_id"] != SELF]
    good, missing = [], []
    for r in measured:
        comp, sel = first_arm(CONFIG, r.get("metric_command", ""))
        rows = latest.get(comp, {})
        why = choose_current(comp, rows.get(sel))
        beaten = [] if why else [x["impl"] for x in leaderboard.challengers(rows, sel, CONFIG.evaluation, log.get(comp))]
        if not why and not beaten:
            good.append(r["solution_id"])
        else:
            missing.append(r["solution_id"] + ("" if why else " (beaten)"))
    total = len(measured) + (len(specs) if rule == "structure_coverage" else 0)
    if rule == "structure_coverage":
        missing += [f"{r['solution_id']} (no metric run yet)" for r in specs]
    value = len(good) / total if total else 0.0
    return dict(value=round(value, 4), good=len(good), total=total, missing=missing)


def read(args, choose=honest_choice, structure=structure_reading):
    """The reading of one sub-metric: for each tier, every arm's reading, the tier's value over its current arms, and the
    share of the tier that is measured."""
    rule = args["rule"]
    log = history()
    out = {"rule": rule, "rule_text": RULES[rule][0], "tiers": {}}
    for tier in TIERS:
        if rule.startswith("structure_"):
            s = structure(rule)
            arms = [dict(arm="structure check", tier=tier, share=1.0, planned=False, current=True, why="", value=s["value"],
                         date=datetime.date.today().isoformat(), sha=head(), cases=s["total"], passed=s["good"],
                         whole=s["total"], cost="0.0", paid=False, estimate=0.0, run="esoi run qar_overall --impl recorded_rows",
                         proxy="", missing=s["missing"])]
        else:
            specs = args.get(tier) or []
            total = sum(s["share"] for s in specs)
            if specs and abs(total - 1.0) > 1e-6:
                raise ValueError(f"{tier} shares of {args.get('what')} sum to {total}, not 1")
            arms = [arm_reading(s, tier, rule, log, choose) for s in specs]
        measured = sum(a["share"] for a in arms if a["current"] and a["value"] is not None)
        got = sum(a["share"] * a["value"] for a in arms if a["current"] and a["value"] is not None)
        out["tiers"][tier] = dict(arms=arms, measured=round(measured, 4), floor=round(got, 4),
                                  value=round(got / measured, 4) if measured else None)
    out["cheap_only"] = not (args.get("full") or rule.startswith("structure_"))
    out["same_tiers"] = bool(args.get("full_same_as_cheap")) or rule.startswith("structure_")
    return out


# ---------------------------------------------------------------- the combined score and the report

def combine(readings, tier):
    """``{headline, floor, ceiling, measured_weight}`` over the sub-metric readings (``{id: reading}``) for one tier."""
    weights, _, unserved = submetric_weights(list(readings))
    floor = measured = 0.0
    for sid, reading in readings.items():
        t = reading["tiers"][tier]
        floor += weights[sid] * t["floor"]
        measured += weights[sid] * t["measured"]
    return dict(headline=round(floor / measured, 3) if measured else None, floor=round(floor, 3),
                ceiling=round(floor + (1.0 - measured), 3), measured_weight=round(measured, 3), unserved=round(unserved, 3))


def money(value):
    if value in ("", None):
        return "?"
    return f"${float(value):.2f}"


def left_out(readings, tier, weights, names, unserved=0.0):
    """One plain sentence naming what the headline of ``tier`` leaves out: each sub-metric with unmeasured weight in that
    tier, by id and name, with how much of its weight is unmeasured."""
    parts = []
    for sid, reading in readings.items():
        gap = weights[sid] * (1.0 - reading["tiers"][tier]["measured"])
        if gap > 5e-4:
            whole = gap >= weights[sid] - 5e-4
            parts.append(f"{sid} {names.get(sid, '')} ("
                         + (f"all of its weight {weights[sid]:.3f}" if whole else f"{gap:.3f} of its weight {weights[sid]:.3f}")
                         + ")")
    if unserved > 5e-4:
        parts.append(f"ideals no sub-metric serves (weight {unserved:.3f})")
    if not parts:
        return "The headline leaves nothing out: every sub-metric is measured on current rows in this tier."
    return "The headline leaves out " + ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1] + "."


def report(rows):
    """The human report: floor and measured weight first, then headline and ceiling, for the QUICK and FULL
    tiers, with a sentence naming what the headline leaves out, a table per sub-metric,
    and what is not measured with the command and estimated cost that would measure it."""
    readings = {r["id"]: r["reading"] for r in rows if r.get("reading")}
    if not readings:
        return "no sub-metric could be read"
    weights, serving, unserved = submetric_weights(list(readings))
    cfg = settings()
    lines = ["", "QAR overall (metric-qar-overall, the authoritative metric of QS1), from recorded rows only; nothing was re-run.",
             f"Read at commit {head()} on {datetime.date.today().isoformat()}. A row counts only when it is current against "
             "that commit; a stale or missing arm is unmeasured, never a pass."]
    names = {r["id"]: r.get("area", "") for r in rows}
    for tier, label in (("cheap", "QUICK (cheap tiers)"), ("full", "FULL (whole input sets, live models where paid)")):
        c = combine(readings, tier)
        head_text = "none (nothing measured)" if c["headline"] is None else f"{c['headline']:.3f}"
        lines.append(f"{label}: FLOOR {c['floor']:.3f} (unmeasured counted 0), {c['measured_weight']:.0%} of the weight "
                     f"measured; headline {head_text} on the measured weight only; ceiling {c['ceiling']:.3f} "
                     "(unmeasured counted 1).")
        lines.append("    " + left_out(readings, tier, weights, names, unserved))
    if unserved:
        lines.append(f"Weight of ideals no sub-metric serves (always unmeasured): {unserved:.0%}.")
    lines += ["", "Per sub-metric (weight from QS1's ideals; value 0..1 by the stated rule; arms: tier, value, date, sha, cost of last run):"]
    not_measured, spend, unknown = [], {t: 0.0 for t in TIERS}, {t: set() for t in TIERS}
    for sid, reading in readings.items():
        name = next((r.get("area") for r in rows if r["id"] == sid), "")
        label = ("cheap-only" if reading["cheap_only"] else
                 "cheap = full (offline, exhaustive; no paid tier exists)" if reading["same_tiers"] else "cheap and full")
        q, f = reading["tiers"]["cheap"], reading["tiers"]["full"]
        qv = "-" if q["value"] is None else f"{q['value']:.3f}"
        fv = "-" if f["value"] is None else f"{f['value']:.3f}"
        lines.append(f"{sid} {name}: weight {weights[sid]:.3f}, {label}; rule: {reading['rule_text']}")
        lines.append(f"    quick {qv} ({q['measured']:.0%} measured), full {fv} ({f['measured']:.0%} measured)")
        shown = {}
        for tier in TIERS:
            for a in reading["tiers"][tier]["arms"]:
                key = (a["arm"], a.get("sha"), a.get("cases"), a.get("current"))
                if key in shown and not a["planned"]:
                    lines[shown[key]] = lines[shown[key]].replace("    cheap ", "    both  ", 1)
                else:
                    if a["planned"]:
                        lines.append(f"    {tier:5} {a['arm']}: NOT BUILT ({a.get('what', '')})")
                    else:
                        v = "-" if a["value"] is None else f"{a['value']:.3f}"
                        state = "current" if a["current"] else f"UNMEASURED ({a['why']})"
                        size = f"{a['passed']}/{a['cases']} of {a['whole']}" if a["cases"] else "no row"
                        lines.append(f"    {tier:5} {a['arm']}: {v} ({size}), {a.get('date') or '-'}, {a.get('sha') or '-'}, "
                                     f"last run {money(a.get('cost'))}{' PAID' if a.get('paid') else ''}, {state}"
                                     + (f"; proxy: {a['proxy']}" if a.get("proxy") else ""))
                        shown[key] = len(lines) - 1
                    if a.get("missing"):
                        lines.append(f"          not counted ({len(a['missing'])}): " + ", ".join(a["missing"][:8])
                                     + (" ..." if len(a["missing"]) > 8 else ""))
                if a["current"]:
                    continue
                est = a.get("estimate")
                est_text = "cost unknown, no recorded row to estimate from" if est is None else f"est. {money(est)}"
                cause = "not built" if a["planned"] else a["why"]
                same = next((n for n in not_measured if n[0] == sid and n[2] == a["arm"] and n[3] == cause
                             and not a.get("paid")), None)
                if same:
                    same[1].append(tier)
                else:
                    not_measured.append((sid, [tier], a["arm"], cause, f"`{a['run'] or '-'}`, {est_text}"))
                if est is None:
                    unknown[tier].add(a["arm"])
                else:
                    spend[tier] += float(est)
    lines += ["", "What is not measured:"]
    lines += [f"  - {sid} {'+'.join(tiers)}: {arm} ({cause}): {rest}" for sid, tiers, arm, cause, rest in not_measured] or ["  - nothing"]
    for tier in TIERS:
        extra = f", plus {', '.join(sorted(unknown[tier]))} with no estimate" if unknown[tier] else ""
        lines.append(f"Estimated spend to measure every unmeasured {tier} arm once: {money(spend[tier])}{extra}.")
    lines += ["", f"Weights per ideal ([balance.{WEIGHTS_KEY}] in esoi.toml), split equally over the sub-metrics that serve it:"]
    for ideal, w in cfg["ideals"].items():
        lines.append(f"  {ideal} {w:.2f} -> {', '.join(serving[ideal]) or 'no sub-metric'}: {cfg['reasons'].get(ideal, '')}")
    return "\n".join(lines)


def load_definitions():
    return json.loads((Path(__file__).resolve().parent / "cases.json").read_text(encoding="utf-8"))
