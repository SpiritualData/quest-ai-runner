"""QAR's terminal words and rules equal the Quest app's: its English pack and its `workTrace.ts`.

Moved here from the hub (which is generic and owns no app's words). QAR registers the Quest wording over the hub's defaults in
`quest_ai_runner/terminal_words.py`; this compares the words in force with the pack.

Needs the sibling `quest-frontend` checkout (the layout of the SpiritualData repository) and, for the rules, `node` and the
checkout's own TypeScript compiler. Without them each test is skipped with the reason; with them a changed sentence or rule in the
app fails here until the hub's table or port follows.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hubofhubs.terminal import strings, trail

import quest_ai_runner.terminal_words  # noqa: F401  (registers the Quest wording)

FRONTEND = Path(__file__).resolve().parents[2] / "quest-frontend"
PACKS = FRONTEND / "src" / "i18n" / "locales" / "en"
SOURCE = FRONTEND / "src" / "utils" / "workTrace.ts"
TSC = FRONTEND / "node_modules" / ".bin" / "tsc"


def flat(obj, prefix=""):
    out = {}
    for k, v in obj.items():
        key = f"{prefix}.{k}" if prefix else k
        out.update(flat(v, key) if isinstance(v, dict) else {key: v})
    return out


needs_packs = pytest.mark.skipif(not (PACKS / "quests.json").exists(), reason="sibling quest-frontend checkout not present")


@needs_packs
def test_every_string_equals_the_app_pack():
    quests = json.loads((PACKS / "quests.json").read_text())
    pack = {f"workTrace.{k}": v for k, v in flat(quests["workTrace"]).items()}
    pack.update({f"replyUpdates.{k}": v for k, v in flat(json.loads((PACKS / "replyUpdates.json").read_text())).items()})
    pack["questAIScreen.continueAnalysis.message"] = quests["questAIScreen"]["continueAnalysis"]["message"]
    hub_only = {'workTrace.recordKind.project'}   # a generic default the app has no key for
    words = {k: v for k, v in strings.table().items() if k not in hub_only}
    different = {k: (v, pack.get(k)) for k, v in words.items() if pack.get(k) != v}
    assert not different, f"hub words differ from the app pack: {different}"
    used = {k for k in pack if k.startswith(("workTrace.summary", "workTrace.removedLinks", "workTrace.group", "workTrace.notice",
                                              "workTrace.recordKind", "workTrace.newestSite", "workTrace.summary", "workTrace.couldNotOpen",
                                              "workTrace.recordsUntitled", "replyUpdates."))}
    assert not used - set(words), f"the app pack has words the hub table lacks: {sorted(used - set(words))}"


def ev(step_id, kind, state="done", counts=None, items=None, affects_user=False, label_key=""):
    return {"v": 1, "step_id": step_id, "kind": kind, "state": state, "counts": counts or {}, "items": items or [],
            "affects_user": affects_user, "label_key": label_key, "solution_id": "x"}


def site(ref, label="", kind="read"):
    return {"ref": ref, "label": label or ref, "kind": kind}


def paper(label, sub):
    return {"ref": f"doi:{label}", "label": label, "sub": sub}


def rec(label, kind="quest", visible=False):
    item = {"ref": label, "label": label, "kind": kind}
    if visible:
        item["visible_to_all"] = True
    return item


R = [rec("Run a half marathon"), rec("Morning pages"), rec("Candle shop"), rec("Spanish")]
FIXTURES = [
    ("one record", [ev("r", "records", counts={"read": 1}, items=R[:1])], False),
    ("two records", [ev("r", "records", counts={"read": 2}, items=R[:2])], False),
    ("four records", [ev("r", "records", counts={"read": 4}, items=R)], False),
    ("untitled record", [ev("r", "records", counts={"read": 3}, items=R[:1])], False),
    ("bare records", [ev("r", "records", counts={"read": 2})], False),
    ("one site", [ev("w", "sources", counts={"read": 1, "found": 3}, items=[site("blog.rust-lang.org", "Rust blog")])], False),
    ("two sites", [ev("w", "sources", counts={"read": 2}, items=[site("a.org"), site("b.org")])], False),
    ("four sites titled", [ev("w", "sources", counts={"read": 4, "failed": 1}, items=[site("a.org", "A page"), site("b.org"), site("c.org"), site("d.org")])], False),
    ("sites no names", [ev("w", "sources", counts={"read": 2})], False),
    ("searched only, cited", [ev("w", "sources", counts={"found": 4, "read": 0}, items=[site("a.org", kind="found")])], False),
    ("searched only, cited four", [ev("w", "sources", counts={"found": 9, "read": 0}, items=[site(f"{c}.org", kind="found") for c in "abcd"])], False),
    ("searched only, uncited", [ev("w", "sources", counts={"found": 4, "read": 0}, items=[])], False),
    ("papers named", [ev("p", "papers", counts={"found": 30, "read": 2}, items=[paper("One", "Lee et al., 2021"), paper("Two", "Ng, 2019")])], False),
    ("papers many", [ev("p", "papers", counts={"found": 9, "read": 5}, items=[paper(str(i), f"Au{i}, 2020") for i in range(5)])], False),
    ("papers found only", [ev("p", "papers", counts={"found": 31})], False),
    ("mixed", [ev("r", "records", counts={"read": 1}, items=R[:1]), ev("w", "sources", counts={"read": 2}, items=[site("a.org"), site("b.org")]),
               ev("p", "papers", counts={"found": 9, "read": 1}, items=[paper("One", "Lee et al., 2021")])], False),
    ("one link removed", [ev("l", "check", counts={"checked": 5, "failed": 1}, items=[{"ref": "https://x.example.com/a?b=1", "label": "x.example.com"}])], False),
    ("two links two hosts", [ev("l", "check", counts={"failed": 2}, items=[{"ref": "a.com", "label": "a.com"}, {"ref": "b.com", "label": "b.com"}])], False),
    ("two links same host", [ev("l", "check", counts={"failed": 2}, items=[{"ref": "a.com", "label": "a.com"}, {"ref": "a.com", "label": "a.com"}])], False),
    ("three links", [ev("l", "check", counts={"failed": 3}, items=[{"ref": "a.com", "label": "a"}, {"ref": "b.com", "label": "b"}, {"ref": "c.com", "label": "c"}])], False),
    ("links no domains", [ev("l", "check", counts={"failed": 2})], False),
    ("slow notice", [ev("n", "notice", affects_user=True, label_key="workTrace.notice.slower")], False),
    ("unknown notice", [ev("n", "notice", affects_user=True, label_key="workTrace.notice.nope")], False),
    ("quiet notice", [ev("n", "notice", affects_user=False)], False),
    ("live newest site", [ev("w", "sources", "running", counts={"read": 2}, items=[site("a.org"), site("https://www.b.org/x?q=1")])], False),
    ("live listed only", [ev("w", "sources", "running", counts={"found": 2, "read": 0}, items=[site("c.com", kind="found")])], False),
    ("live papers", [ev("p", "papers", "running", counts={"read": 1}, items=[paper("One", "Lee et al., 2021")])], False),
    ("replaced step", [ev("w", "sources", "running", counts={"read": 1}, items=[site("a.org")]), ev("w", "sources", "done", counts={"read": 2}, items=[site("a.org"), site("b.org")])], False),
    ("bad event dropped", [{"step_id": "", "state": "done"}, {"state": "done"}, ev("w", "sources", counts={"read": 1}, items=[site("a.org")])], False),
    ("shared private", [ev("r", "records", counts={"read": 4}, items=R)], True),
    ("shared one visible", [ev("r", "records", counts={"read": 1}, items=[rec("Launch the candle shop", visible=True)])], True),
    ("shared two visible", [ev("r", "records", counts={"read": 2}, items=[rec("Launch the candle shop", visible=True), rec("Team offsite", visible=True)])], True),
    ("shared mixed", [ev("r", "records", counts={"read": 4}, items=[rec("Launch the candle shop", visible=True), rec("Mine")] )], True),
    ("kinds and titles", [ev("r", "records", counts={"read": 4}, items=[rec("Plan A", "goal"), rec("Plan B", "reflection"), rec("Plan C", "note"), rec("Plan D")])], False),
    ("not titles", [ev("r", "records", counts={"read": 3}, items=[rec("Conversation turn"), rec("notes/plan.md"), rec("Cut off text…")])], False),
]

NODE = r"""
const fs = require('fs'); const path = require('path');
const [compiled, packFile, fixtureFile] = process.argv.slice(2);
const w = require(path.join(compiled, 'utils', 'workTrace.js'));
const pack = JSON.parse(fs.readFileSync(packFile, 'utf8')).workTrace;
function t(key, params = {}) {
  const rel = key.replace(/^quests\.workTrace\./, '');
  const at = (k) => k.split('.').reduce((o, p) => (o == null ? o : o[p]), pack);
  let value = at(rel);
  if (value === undefined && typeof params.count === 'number') value = at(`${rel}_${params.count === 1 ? 'one' : 'other'}`);
  if (value === undefined) value = at(`${rel}_other`);
  if (typeof value !== 'string') { if (params.defaultValue !== undefined) return String(params.defaultValue); throw new Error('missing ' + key); }
  return value.replace(/\{\{(\w+)\}\}/g, (_, n) => String(params[n]));
}
const out = {};
for (const [name, events, shared] of JSON.parse(fs.readFileSync(fixtureFile, 'utf8'))) {
  let steps = [];
  for (const e of events) steps = w.applyTraceEvent(steps, e);
  const view = shared ? w.redactForShared(steps) : steps;
  out[name] = { live: w.liveText(steps, t), summary: w.summaryText(view, t, shared), visible: w.hasVisibleWork(view, shared),
                expandable: w.hasExpandableDetail(view, shared),
                groups: w.expandedGroups(view, t, shared).map((g) => ({ id: g.id, title: g.title, rows: g.rows, notes: g.notes })) };
}
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def app_answers(tmp_path_factory):
    if not SOURCE.exists():
        pytest.skip("sibling quest-frontend checkout not present")
    if shutil.which("node") is None or not TSC.exists():
        pytest.skip("node or the quest-frontend TypeScript compiler is not installed")
    work = tmp_path_factory.mktemp("worktrace")
    compiled = work / "out"
    subprocess.run([str(TSC), str(SOURCE), "--outDir", str(compiled), "--module", "commonjs", "--target", "es2020", "--skipLibCheck"],
                   capture_output=True, cwd=FRONTEND)   # type errors elsewhere do not stop the emit
    if not (compiled / "utils" / "workTrace.js").exists():
        pytest.skip("workTrace.ts did not compile")
    script, fixtures = work / "run.js", work / "fixtures.json"
    script.write_text(NODE)
    fixtures.write_text(json.dumps(FIXTURES))
    done = subprocess.run(["node", str(script), str(compiled), str(PACKS / "quests.json"), str(fixtures)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.parametrize("name,events,shared", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_rules_and_words_equal_workTrace_ts(app_answers, name, events, shared):
    steps = []
    for e in events:
        steps = trail.apply_event(steps, e)
    view = trail.redact_for_shared(steps) if shared else steps
    mine = {
        "live": trail.live_text(steps), "summary": trail.summary_text(view, shared), "visible": trail.has_visible_work(view, shared),
        "expandable": trail.has_expandable_detail(view, shared),
        "groups": [{"id": g["id"], "title": g["title"], "rows": g["rows"], "notes": g["notes"]} for g in trail.expanded_groups(view, shared)],
    }
    assert mine == app_answers[name]
