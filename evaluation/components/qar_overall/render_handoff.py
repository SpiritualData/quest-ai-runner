"""Render the QAR handoff page from the recorded results and the ESOI records. Offline: no model call, no arm is run.

Run from the repository root (the page is regenerated whole each time, never hand-edited):

    python evaluation/components/qar_overall/render_handoff.py --content <private-dir>/handoff-content.json
    python evaluation/components/qar_overall/render_handoff.py --content <content.json> --out <page>.html
    python evaluation/components/qar_overall/render_handoff.py --content <content.json> --check      # exit 1 when stale
    python evaluation/components/qar_overall/render_handoff.py --content <content.json> \
        --record end_to_end "own_data 2/2, multistep 1/2"      # store a per-kind split from a run's summary line

Defaults use relative placeholders only. ``--content`` (or ``QAR_HANDOFF_CONTENT``) is a small JSON file kept OUTSIDE this
public repository: the maintainer's decisions, judgement calls, unfinished-work list, notes and the per-case splits the
results log does not carry. ``--out`` (or ``QAR_HANDOFF_OUT``) defaults to the content file's ``page`` key, resolved
relative to the content file, then to ``handoff.html`` in the current directory.

What it reads, all committed records: the combined metric (``combine.read`` over ``cases.json``, QME1 to QME9), each
conceptual part's solution, metric and latest recorded row with its currency, the prompt-spend audit (traces and
``justified_model_spend.score.judge``), the end-to-end and proactive-guidance rows and examples, the hub registry
(``esoi/hubs.csv``) and the freshness of every row. Content keys (all optional): ``title``, ``audience``, ``written``,
``summary``, ``next``, ``cards`` (title, body), ``decisions`` (decision, answer, whose: joshua, /joshua or metric),
``unfinished`` (item, state), ``notes`` (list of lines), ``pass_log`` (date, summary), ``links`` (label, url),
``breakdowns`` (end_to_end, proactive_level, proactive_class: row_date, parts as ``{label: [passed, cases]}``), ``page``.
The text never contains an em dash: every dash is replaced on output."""
import argparse
import csv
import datetime
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import common  # noqa: E402
from esoi.registry import first_arm, load as load_registry  # noqa: E402
from qar_overall import combine  # noqa: E402

ROOT = Path(common.CONFIG.root)
COMPONENTS = ROOT / "evaluation" / "components"
DEFAULT_OUT = "handoff.html"

# Each conceptual part of the anatomy (docs/anatomy.md) and the solution that specifies it.
PARTS = [
    ("Mind", "keep-a-working-stream"), ("Subconscious (includes Intuition)", "admit-to-the-mind"),
    ("Science", "know-the-world"), ("Critical thinking", "think-critically"), ("World", "act-in-the-world"),
    ("Body", "act-as-the-body"), ("Higher mind", "hold-the-long-view"), ("Overseer", "watch-a-run"),
    ("Rep selection", "select-a-rep"), ("Proactive guidance", "guide-toward-the-long-term-goal"),
    ("Hub of hubs", "use-open-source-hubs"), ("End to end", "answer-end-to-end"), ("Prompt audit", "justify-model-calls"),
]
STALE_DAYS = 14   # a hub finding older than this is called stale on the page


# ---------------------------------------------------------------- collecting the records

def git(*args):
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def latest_row(log, component, impl):
    rows = log.get(component, {}).get(impl, [])
    return rows[-1] if rows else None


def row_facts(log, component, impl):
    row = latest_row(log, component, impl)
    if row is None:
        return dict(component=component, impl=impl, state="not built", why="no recorded row", passed=0, cases=0, date="")
    why = combine.current(component, row)
    return dict(component=component, impl=impl, state="stale" if why else "measured", why=why, date=row.get("date", ""),
                passed=int(combine.num(row, "passed")), cases=int(combine.num(row, "cases")), sha=row.get("git_sha", ""),
                cost=row.get("cost_usd", ""), model=row.get("model", ""))


def part_facts(reg, log):
    solutions = {r["solution_id"]: r for r in reg.solutions}
    out = []
    for part, sid in PARTS:
        sol = solutions.get(sid)
        spec = any(r["solution_id"] == sid for r in reg.specifications)
        entry = dict(part=part, solution=sid, metric=(sol or {}).get("metric_ids", "") if sol else "", selected="")
        arm = None
        if sol and sol.get("metric_command"):
            try:
                arm = first_arm(common.CONFIG, sol["metric_command"])
            except Exception:  # noqa: BLE001  a malformed command is shown as not built, never a crash
                arm = None
        if arm and arm[0] and arm[1]:
            entry.update(row_facts(log, arm[0], arm[1]))
            entry["selected"] = arm[1]
        else:
            entry.update(state="not built", why="specification only" if spec else "no metric command", passed=0, cases=0, date="")
        out.append(entry)
    return out


def submetric_facts(log):
    definitions = combine.load_definitions()
    readings, names = {}, {}
    for case in definitions:
        readings[case["id"]] = combine.read(case["args"])
        names[case["id"]] = case.get("area", "")
    weights, _, unserved = combine.submetric_weights(list(readings))
    cfg = combine.settings()
    subs = []
    for sid, reading in readings.items():
        tiers = {}
        for tier in combine.TIERS:
            t = reading["tiers"][tier]
            tiers[tier] = dict(value=t["value"], measured=t["measured"], floor=t["floor"])
        arms = {}
        for tier in combine.TIERS:
            for a in reading["tiers"][tier]["arms"]:
                arms[a["arm"]] = dict(arm=a["arm"], current=a["current"], planned=a["planned"], why=a["why"], date=a.get("date", ""))
        subs.append(dict(id=sid, name=names[sid], weight=weights[sid], tiers=tiers, arms=list(arms.values()),
                         cheap_only=reading["cheap_only"]))
    overall = {t: combine.combine(readings, t) for t in combine.TIERS}
    all_arms = {a["arm"]: a for s in subs for a in s["arms"] if not a["planned"]}
    stale = [a for a in all_arms.values() if not a["current"]]
    reasons = {}
    for a in stale:
        key = re.sub(r"^stale, ", "", re.sub(r"since [0-9a-f]+", "since it was recorded", a["why"] or "not current"))
        reasons[key] = reasons.get(key, 0) + 1
    return dict(subs=subs, overall=overall, bar=cfg["bar"], unserved=unserved,
                stale=dict(count=len(stale), total=len(all_arms), reasons=reasons, arms=sorted(a["arm"] for a in stale)))


def prompt_facts():
    from justified_model_spend import score
    cases = json.loads((COMPONENTS / "justified_model_spend" / "cases.json").read_text(encoding="utf-8"))
    rows = []
    for case in cases:
        verdict = score.judge(case, ablate=True, check_budget=True, check_owner=True)
        s = verdict.get("stats")
        rows.append(dict(spec=case["spec_id"], ok=verdict["verdict"] == "pass", reasons=verdict["reasons"],
                         avg_spend=verdict.get("avg_spend", 0.0),
                         stats=None if not s else dict(cost=s["cost_per_call"], tin=s["tokens_in"], tout=s["tokens_out"],
                                                       sec=s["seconds_per_call"], fire=s["fire_rate"], model=s.get("model") or "")))
    try:
        unmeasured = score.unmeasured()
    except Exception:  # noqa: BLE001
        unmeasured = []
    return dict(rows=rows, unmeasured=len(unmeasured), total=len(rows) + len(unmeasured))


def hub_facts(today):
    path = ROOT / "esoi" / "hubs.csv"
    rows = []
    for r in csv.DictReader(path.open(encoding="utf-8")) if path.is_file() else []:
        try:
            age = (today - datetime.date.fromisoformat(r["last_researched"])).days
        except ValueError:
            age = None
        rows.append(dict(area=r["area"], status=r["status"], age=age, hubs=len([h for h in r["current_hubs"].split(";") if h.strip()]),
                         candidates=len([h for h in r["candidate_hubs"].split(";") if h.strip()])))
    return rows


def kind_counts():
    cases = json.loads((COMPONENTS / "end_to_end" / "cases.json").read_text(encoding="utf-8"))
    counts = {}
    for c in cases:
        counts[c["kind"]] = counts.get(c["kind"], 0) + 1
    return counts


def proactive_counts():
    cases = json.loads((COMPONENTS / "proactive_guidance" / "cases.json").read_text(encoding="utf-8"))
    level, klass = {}, {}
    for c in cases:
        level[c["level"]] = level.get(c["level"], 0) + 1
        klass[c["class"]] = klass.get(c["class"], 0) + 1
    return level, klass


def examples():
    path = COMPONENTS / "proactive_guidance" / "handoff_examples.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    chosen, seen = [], set()
    for e in data.get("examples", []):
        if e["example"] not in seen:
            seen.add(e["example"])
            chosen.append(e)
    return chosen


def latest_results_commit_time():
    stamp = git("log", "-1", "--format=%ct", "--", str(Path(common.CONFIG.results_path).resolve()))
    return datetime.datetime.fromtimestamp(int(stamp), datetime.timezone.utc) if stamp else None


def collect(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    log = combine.history()
    reg = load_registry(common.CONFIG)
    data = dict(generated=now.strftime("%Y-%m-%d %H:%M"), generated_iso=now.isoformat(timespec="seconds"),
                commit=combine.head(), today=now.date().isoformat())
    data.update(submetric_facts(log))
    data["parts"] = part_facts(reg, log)
    data["prompts"] = prompt_facts()
    data["hubs"] = hub_facts(now.date())
    data["e2e"] = dict(row_facts(log, "end_to_end", "qar_live"), kinds=kind_counts())
    level, klass = proactive_counts()
    data["proactive"] = dict(arms=[row_facts(log, "proactive_guidance", i) for i in ("guided_v2", "no_nudge")], levels=level, classes=klass)
    data["examples"] = examples()
    data["row_times"] = dict(results_committed=(latest_results_commit_time() or now).isoformat(timespec="seconds"))
    return data


# ---------------------------------------------------------------- rendering

CSS = """:root{--bg:#fafaf7;--fg:#1c1c1a;--mut:#66665f;--card:#fff;--line:#e3e3dc;--ok:#1d7a46;--warn:#a6620b;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecece6;--mut:#a3a39a;--card:#1f1f1c;--line:#34342f;--ok:#5cc58a;--warn:#e0a24a;--bad:#f08178}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:820px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.5rem;margin:0 0 4px}h2{font-size:1.1rem;margin:28px 0 8px}
.sub{color:var(--mut);margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0}
.big{display:flex;gap:12px;flex-wrap:wrap}.big div{flex:1 1 130px}
.n{font-size:1.7rem;font-weight:650}.l{color:var(--mut);font-size:.85rem}
.wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.92rem}
td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.tag{font-size:.75rem;border:1px solid var(--line);border-radius:6px;padding:1px 6px;color:var(--mut);white-space:nowrap}
.tag.ok{border-color:var(--ok)}.tag.warn{border-color:var(--warn)}.tag.bad{border-color:var(--bad)}
.banner{border:1px solid var(--warn);border-radius:10px;padding:10px 14px;margin:12px 0;color:var(--warn)}
.ex{border-left:3px solid var(--line);padding:2px 12px;margin:10px 0}.ex p{margin:4px 0}
.foot{color:var(--mut);font-size:.82rem;margin-top:32px}
a{color:inherit}"""


def esc(value):
    return html.escape("" if value is None else str(value))


def clean(text):
    """No em or en dash may reach a reader: each becomes a comma."""
    return re.sub(r"\s*[—–]\s*", ", ", text)


def num(value, places=2):
    return "-" if value is None else f"{value:.{places}f}"


def pct(value):
    return f"{value:.0%}"


def tone(value, bar, measured=1.0):
    if value is None or measured < 1.0:
        return "warn"
    return "ok" if value >= bar else "bad"


def tag(text, kind=""):
    return f'<span class="tag {kind}">{esc(text)}</span>'


def table(head, rows, empty="nothing recorded"):
    if not rows:
        return f'<p class="sub">{esc(empty)}</p>'
    th = "".join(f"<th>{esc(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="wrap"><table><tr>{th}</tr>{body}</table></div>'


def split_cell(breakdown, label, row_date, fallback):
    """``passed/cases`` of one label from a stored breakdown, or the fallback; flagged when it is from an earlier run."""
    if breakdown and label in breakdown.get("parts", {}):
        passed, cases = breakdown["parts"][label]
        old = breakdown.get("row_date") != row_date
        return f"{passed}/{cases}" + (" " + tag("earlier run", "warn") if old else "")
    return fallback


def section_banner(data):
    s = data["stale"]
    if not s["count"]:
        return ""
    worst = max(s["reasons"].items(), key=lambda kv: kv[1])[0]
    return (f'<div class="banner"><b>{s["count"]} of {s["total"]} recorded arms are stale</b> ({esc(worst)}). Stale arms count as '
            f"unmeasured, so the floor is lower than the work is. The daily pass re-records them first.</div>")


def section_cards(data):
    q, f = data["overall"]["cheap"], data["overall"]["full"]
    bar = data["bar"]

    def card(label, value, kind):
        return f'<div><div class="n {kind}">{esc(value)}</div><div class="l">{esc(label)}</div></div>'
    cards = [card(f"Quick floor, {pct(q['measured_weight'])} measured", f"{q['floor']:.3f}", tone(q["floor"], bar, q["measured_weight"])),
             card("Quick headline, measured part", "-" if q["headline"] is None else f"{q['headline']:.3f}", tone(q["headline"], bar)),
             card(f"Full floor, {pct(f['measured_weight'])} measured", f"{f['floor']:.3f}", tone(f["floor"], bar, f["measured_weight"])),
             card("Full headline, measured part", "-" if f["headline"] is None else f"{f['headline']:.3f}", tone(f["headline"], bar)),
             card("Ceiling if all unmeasured passed", f"{q['ceiling']:.3f}", "")]
    return '<h2>Overall QAR metric</h2><div class="card big">' + "".join(cards) + "</div>"


def sub_state(sub, bar):
    q = sub["tiers"]["cheap"]
    missing = [a for a in sub["arms"] if not a["current"]]
    if q["measured"] >= 1.0 and (q["value"] or 0) >= bar:
        return "ok", "above the bar"
    if q["measured"] >= 1.0:
        return "bad", f"below the {bar:g} bar"
    named = ", ".join(sorted({a["arm"].split("/")[-1] + (" (not built)" if a["planned"] else "") for a in missing})[:3])
    return "warn", f"{pct(q['measured'])} measured; {named} not counted" if named else f"{pct(q['measured'])} measured"


def section_submetrics(data):
    rows = []
    for s in data["subs"]:
        q, f = s["tiers"]["cheap"], s["tiers"]["full"]
        kind, text = sub_state(s, data["bar"])
        rows.append([f"{esc(s['id'])} {esc(s['name'])}", f"{num(q['value'])}", f"{num(f['value'])}",
                     f'<span class="{kind}">{esc(text)}</span>'])
    return "<h2>Sub-metrics</h2>" + table(["Sub-metric", "Quick", "Full", "State"], rows)


def part_status(p):
    if p["state"] == "measured":
        return tag("measured", "ok")
    if p["state"] == "stale":
        return tag("stale", "warn")
    return tag("not built", "bad")


def section_parts(data):
    rows = []
    for p in data["parts"]:
        latest = f"{p['passed']}/{p['cases']}" + (f" on {esc(p['date'])}" if p.get("date") else "") if p["cases"] else "-"
        why = f' <span class="l">{esc(re.sub("^stale, ", "", p["why"]))}</span>' if p["state"] != "measured" and p.get("why") else ""
        rows.append([esc(p["part"]), esc(p["solution"]), esc(p["metric"] or "-"), latest, part_status(p) + why])
    return ("<h2>Every part: metric and status</h2>"
            + table(["Part", "Solution", "Metric", "Latest result", "Status"], rows))


def section_e2e(data, content):
    e = data["e2e"]
    bd = (content.get("breakdowns") or {}).get("end_to_end")
    rows = []
    for kind, n in sorted(e["kinds"].items()):
        rows.append([esc(kind), str(n), split_cell(bd, kind, e.get("date"), "-")])
    head = f"{e['passed']}/{e['cases']} turns pass" if e["cases"] else "no run recorded"
    note = "" if bd else '<p class="sub">The per-kind split is not in the results log; the daily pass stores it with --record.</p>'
    return (f'<h2>End to end</h2><p>{esc(head)}{" on " + esc(e["date"]) if e.get("date") else ""} {part_status(e)}</p>'
            + table(["Kind", "Cases", "Passed"], rows) + note)


def section_proactive(data, content):
    p = data["proactive"]
    arms = []
    for a in p["arms"]:
        arms.append([esc(a["impl"]), f"{a['passed']}/{a['cases']}" if a["cases"] else "-", esc(a.get("date", "")), part_status(a)])
    row_date = (p["arms"][0] or {}).get("date")
    out = "<h2>Proactive guidance</h2>" + table(["Arm", "Passed", "Recorded", "Status"], arms)
    for key, title, counts in (("proactive_level", "Intent level", p["levels"]), ("proactive_class", "Class", p["classes"])):
        bd = (content.get("breakdowns") or {}).get(key)
        rows = [[esc(label), str(n), split_cell(bd, label, row_date, "-")] for label, n in sorted(counts.items())]
        out += table([title, "Cases", "Passed (candidate arm)"], rows)
    exs = data["examples"]
    if exs:
        out += "<h3>Examples</h3>"
        for e in exs:
            out += (f'<div class="ex"><p>{tag(e["example"].replace("_", " "))} {tag(e["class"])} {tag("level " + e["intent_level"])}</p>'
                    f'<p><b>Asked:</b> {esc(e["request"])}</p><p><b>Reply:</b> {esc(e["response"])}</p>'
                    f'<p class="l">{esc(e["why"])}</p></div>')
    return out


def section_prompts(data):
    pr = data["prompts"]
    rows = []
    for r in pr["rows"]:
        s = r["stats"]
        if s:
            cost = f"${s['cost']['avg']:.5f} / ${s['cost']['max']:.5f}"
            tokens = f"{s['tin']['avg']:.0f} / {s['tin']['max']:.0f} in, {s['tout']['avg']:.0f} / {s['tout']['max']:.0f} out"
            secs = f"{s['sec']['avg']:.1f} / {s['sec']['max']:.1f} s"
        else:
            cost = tokens = secs = "-"
        why = "; ".join(r["reasons"])[:140]
        rows.append([esc(r["spec"]), tag("justified", "ok") if r["ok"] else tag("not yet", "bad"), cost, tokens, secs, esc(why)])
    held = sum(1 for r in pr["rows"] if r["ok"])
    return (f"<h2>Prompt spend audit</h2><p>{len(pr['rows'])} of {pr['total']} registered prompts measured, {held} justified "
            f"({pr['unmeasured']} unmeasured). Average / maximum per call, from evaluation traces.</p>"
            + table(["Prompt", "Verdict", "Cost", "Tokens", "Seconds", "Why not"], rows))


def section_hubs(data):
    qme9 = next((s for s in data["subs"] if s["id"] == "QME9"), None)
    rows = []
    for h in data["hubs"]:
        age = h["age"]
        old = age is None or age > STALE_DAYS
        rows.append([esc(h["area"]), esc(h["status"]), str(h["hubs"]), str(h["candidates"]),
                     tag("never" if age is None else f"{age} d ago", "warn" if old else "ok")])
    head = ""
    if qme9:
        q = qme9["tiers"]["cheap"]
        head = f"<p>QME9 hub-of-hubs score {num(q['value'])}, {pct(q['measured'])} measured.</p>"
    return "<h2>Hub of hubs</h2>" + head + table(["Area", "Status", "Hubs", "Candidates", "Researched"], rows)


def is_open(decision):
    return str(decision.get("answer", "")).lstrip().lower().startswith("open")


def decision_row(d):
    label = {"joshua": ("Your answer", "ok"), "/joshua": ("/joshua", ""), "metric": ("metric", "")}
    text, kind = label.get(str(d.get("whose", "")).lower(), (d.get("whose", ""), ""))
    return [esc(d["decision"]), esc(d["answer"]), tag(text, kind)]


def section_open_decisions(content):
    """The decisions still waiting for an answer, at the top of the page so nobody scrolls for them."""
    rows = [decision_row(d) for d in content.get("decisions", []) if is_open(d)]
    if not rows:
        return ""
    return '<h2>Open decisions</h2><div class="card">' + table(["Decision", "Recommendation and default", "Whose"], rows) + "</div>"


def section_decisions(content):
    rows = [decision_row(d) for d in content.get("decisions", []) if not is_open(d)]
    return "<h2>Decided</h2>" + table(["Decision", "Answer", "Whose"], rows, "no decisions recorded")


def section_unfinished(content):
    rows = [[esc(u["item"]), esc(u["state"])] for u in content.get("unfinished", [])]
    return ("<h2>Unfinished work (the daily pass picks these up, weakest first)</h2>"
            + table(["Item", "Where it stands"], rows, "nothing unfinished recorded"))


def section_extras(content):
    out = ""
    for c in content.get("cards", []):
        out += f'<h2>{esc(c["title"])}</h2><div class="card"><p>{esc(c["body"])}</p></div>'
    if content.get("notes"):
        out += "<h2>Notes</h2><ul>" + "".join(f"<li>{esc(n)}</li>" for n in content["notes"]) + "</ul>"
    if content.get("pass_log"):
        rows = [[esc(p["date"]), esc(p["summary"])] for p in content["pass_log"][-6:][::-1]]
        out += "<h2>Recent passes</h2>" + table(["Date", "Summary"], rows)
    return out


def render(data, content):
    title = content.get("title", "QAR consciousness handoff")
    audience = content.get("audience", "")
    sub = " ".join(x for x in [f"For {audience}." if audience else "",
                               "Every number comes from recorded rows at the latest commit of the Quest AI Runner."] if x)
    parts = [f"<h1>{esc(title)}</h1>", f'<p class="sub">{esc(sub)}</p>', section_open_decisions(content), section_banner(data)]
    if content.get("summary"):
        parts.append(f"<p>{esc(content['summary'])}</p>")
    parts += [section_cards(data), section_submetrics(data)]
    if content.get("next"):
        parts.append(f"<p><b>Next to raise it:</b> {esc(content['next'])}</p>")
    parts += [section_parts(data), section_e2e(data, content), section_proactive(data, content), section_prompts(data),
              section_hubs(data), section_decisions(content), section_unfinished(content), section_extras(content)]
    links = content.get("links", [])
    if links:
        parts.append('<p class="sub">' + " | ".join(f'<a href="{esc(l["url"])}">{esc(l["label"])}</a>' for l in links) + "</p>")
    parts.append(f'<p class="foot">Generated {esc(data["generated"])} UTC from commit {esc(data["commit"])}.</p>')
    page = (f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<meta name="qar-generated" content="{esc(data["generated_iso"])}">\n<title>{esc(title)}</title>\n'
            f"<style>\n{CSS}\n</style>\n</head>\n<body>\n<main>\n" + "\n".join(p for p in parts if p) + "\n</main>\n</body>\n</html>\n")
    return clean(page)


# ---------------------------------------------------------------- command line

def paths(args):
    content = args.content or os.environ.get("QAR_HANDOFF_CONTENT")
    if not content:
        sys.exit("render_handoff: give --content <file> (or set QAR_HANDOFF_CONTENT)")
    content_path = Path(content).resolve()
    data = json.loads(content_path.read_text(encoding="utf-8")) if content_path.is_file() else {}
    out = args.out or os.environ.get("QAR_HANDOFF_OUT")
    if not out and data.get("page"):
        out = str((content_path.parent / data["page"]).resolve())
    return content_path, data, Path(out or DEFAULT_OUT)


def check(out):
    """Exit status 1 when the page is missing or was generated before the latest committed results row."""
    if not out.is_file():
        print(f"stale: {out} does not exist")
        return 1
    found = re.search(r'name="qar-generated" content="([^"]+)"', out.read_text(encoding="utf-8"))
    latest = latest_results_commit_time()
    if not found:
        print("stale: the page has no generated stamp")
        return 1
    made = datetime.datetime.fromisoformat(found.group(1))
    if latest and made < latest:
        print(f"stale: page generated {made:%Y-%m-%d %H:%M} UTC, results committed {latest:%Y-%m-%d %H:%M} UTC")
        return 1
    print(f"fresh: page generated {made:%Y-%m-%d %H:%M} UTC")
    return 0


def record(content_path, content, name, summary):
    """Store a per-case split such as ``own_data 2/2, multistep 1/2`` against the latest row it came from."""
    keys = {"end_to_end": ("end_to_end", "qar_live"), "proactive_level": ("proactive_guidance", "guided_v2"),
            "proactive_class": ("proactive_guidance", "guided_v2")}
    if name not in keys:
        sys.exit(f"render_handoff: --record NAME is one of {', '.join(keys)}")
    row = latest_row(combine.history(), *keys[name]) or {}
    parts = {}
    for label, passed, cases in re.findall(r"([a-z_]+)\s+(\d+)\s*/\s*(\d+)", summary):
        parts[label] = [int(passed), int(cases)]
    if not parts:
        sys.exit("render_handoff: no 'label passed/cases' pairs found in the summary")
    content.setdefault("breakdowns", {})[name] = dict(row_date=row.get("date", ""), parts=parts)
    content_path.write_text(json.dumps(content, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"recorded {name}: {parts}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--content", help="private content JSON (or QAR_HANDOFF_CONTENT)")
    ap.add_argument("--out", help="page to write (or QAR_HANDOFF_OUT; default the content file's 'page')")
    ap.add_argument("--check", action="store_true", help="exit 1 when the page is older than the latest committed results row")
    ap.add_argument("--record", nargs=2, metavar=("NAME", "SUMMARY"), help="store a per-case split in the content file")
    args = ap.parse_args(argv)
    content_path, content, out = paths(args)
    if args.check:
        return check(out)
    if args.record:
        return record(content_path, content, *args.record)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(collect(), content), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
