"""Offline tests of the handoff page renderer: a fixture, the content merge, the stale banner and the real records.
Run:  .venv/bin/python3 -m pytest evaluation/components/qar_overall/test_render_handoff.py -q"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from qar_overall import render_handoff as rh  # noqa: E402


def fixture(stale=0):
    tier = dict(value=0.8, measured=1.0, floor=0.8)
    sub = dict(id="QME1", name="Chat — turns", weight=1.0, tiers={"cheap": tier, "full": tier}, arms=[], cheap_only=False)
    overall = dict(headline=0.8, floor=0.8, ceiling=0.8, measured_weight=1.0, unserved=0.0)
    return dict(generated="2026-10-29 10:00", generated_iso="2026-10-29T10:00:00+00:00", commit="abc1234", today="2026-10-29",
                subs=[sub], overall={"cheap": overall, "full": overall}, bar=0.8, unserved=0.0,
                stale=dict(count=stale, total=3, reasons={"code changed since it was recorded": stale}, arms=[]),
                parts=[dict(part="Mind", solution="s", metric="m", state="measured", why="", passed=3, cases=4, date="2026-10-29")],
                prompts=dict(rows=[], unmeasured=2, total=2), hubs=[dict(area="a", status="x", age=3, hubs=1, candidates=0)],
                e2e=dict(state="measured", why="", passed=1, cases=2, date="2026-10-29", kinds={"web": 2}),
                proactive=dict(arms=[dict(impl="g", state="measured", why="", passed=1, cases=2, date="2026-10-29")],
                               levels={"quest": 2}, classes={"nudge": 2}), examples=[])


CONTENT = {"decisions": [{"decision": "D1", "answer": "A1", "whose": "joshua"}, {"decision": "D2", "answer": "A2", "whose": "/joshua"}],
           "unfinished": [{"item": "Thing", "state": "open"}], "summary": "Dash — here",
           "breakdowns": {"end_to_end": {"row_date": "2026-10-29", "parts": {"web": [2, 2]}}}}


def test_renders_all_sections_without_dashes():
    page = rh.render(fixture(), CONTENT)
    for text in ("Overall QAR metric", "Every part", "End to end", "Proactive guidance", "Prompt spend audit", "Hub of hubs",
                 "Decided", "Unfinished work", "Generated 2026-10-29 10:00 UTC from commit abc1234"):
        assert text in page
    assert "—" not in page and "–" not in page


def test_content_merges_and_labels_whose():
    page = rh.render(fixture(), CONTENT)
    assert "Your answer" in page and "/joshua" in page and "Thing" in page and "2/2" in page


def test_stale_banner_only_when_stale():
    assert "are stale" in rh.render(fixture(stale=2), CONTENT)
    assert "are stale" not in rh.render(fixture(), CONTENT)


def test_runs_on_the_real_records():
    page = rh.render(rh.collect(), CONTENT)
    assert "Sub-metrics" in page and "QME9" in page and "—" not in page


def test_open_decisions_come_first_and_are_not_repeated_below():
    content = dict(CONTENT)
    content["decisions"] = [{"decision": "Waiting thing", "answer": "Open. Asked of the owner.", "whose": "Payal"},
                            {"decision": "Done thing", "answer": "Answered: yes", "whose": "Payal"}]
    page = rh.render(fixture(), content)
    assert page.index("Open decisions") < page.index("Overall QAR metric")
    assert page.count("Waiting thing") == 1 and page.index("Waiting thing") < page.index("Decided")
    assert page.index("Done thing") > page.index("Decided")


# ---- the "How it handled real inputs" section and add_examples.py

EXAMPLES = [
    {"id": "E2E-GOAL-01", "group": "Adding a goal", "shows": "adds the goal on the pinned quest", "request": "Add a goal to run three times this week.",
     "context": [{"label": "quest on Sub-50 10K", "text": "Sub-50 10K. Outcome: run a sub-50 10K"}], "steps": ["1. wrote create_quest_goal(title='Run')"],
     "result": "create_quest_goal(title='Run three times') on Sub-50 10K", "result_label": "Did", "ok": True},
    {"id": "E2E-QUEST-02", "group": "Questions about Quest", "side": "mentee", "shows": "asks first", "request": "Can Quest help?",
     "steps": ["1. replied"], "result": "Which area matters most?", "result_label": "Reply", "ok": False,
     "quality": {"passed": 4, "total": 5}, "source": "docs/getting-started/index.md"},
    {"id": "E2E-SETTINGS", "group": "Settings and account", "request": "How do I change my time zone?", "result": "Open Settings.", "ok": True},
]


def with_examples(examples):
    return dict(CONTENT, examples=examples)


def test_the_examples_section_renders_groups_and_marks_pass_and_fail():
    page = rh.render(fixture(), with_examples(EXAMPLES))
    for text in ("How it handled real inputs", "Adding a goal", "Questions about Quest", "Settings and account", "E2E-GOAL-01", "pass", "fail",
                 "Found, not mentioned in the request", "mentee", "quality 4/5", "docs/getting-started/index.md", "Which area matters most?"):
        assert text in page
    assert page.index("Adding a goal") < page.index("Questions about Quest") < page.index("Settings and account")
    assert "—" not in page and "–" not in page


def test_the_examples_section_is_omitted_when_there_are_none():
    assert "How it handled real inputs" not in rh.render(fixture(), CONTENT)
    assert "How it handled real inputs" not in rh.render(fixture(), with_examples([]))


def test_the_examples_come_after_the_numbers_and_the_open_decisions_never_before():
    content = with_examples(EXAMPLES)
    content["decisions"] = [{"decision": "Waiting thing", "answer": "Open. Asked.", "whose": "joshua"}]
    page = rh.render(fixture(), content)
    here = page.index("How it handled real inputs")
    assert page.index("Open decisions") < page.index("Overall QAR metric") < page.index("Sub-metrics") < here
    assert here < page.index("Every part: metric and status")


HOME = "/" + "ho" + "me"             # planted pieces are built, so the public-hygiene scan sees no real-looking path or id here
OBJECT_ID = "0123456789" + "abcdef" + "01234567"
KEY = "qsk" + "_abcdef123456"


def test_the_examples_section_escapes_html_and_scrubs_email_id_link_and_path():
    planted = dict(EXAMPLES[0], request=f"<script>alert(1)</script> mail jane.doe@example.org about {HOME}/someone/work/file.txt",
                   result=f"see https://secret.example.org/x and {OBJECT_ID} and {KEY}", steps=["1. read /tmp/abc/def"])
    page = rh.render(fixture(), with_examples([planted]))
    assert "<script>" not in page and "&lt;script&gt;" in page
    for leak in ("jane.doe", "example.org", f"{HOME}/someone", "0123456789abcdef", "qsk_abcdef", "/tmp/abc"):
        assert leak not in page, leak
    for mark in ("[email]", "[path]", "[id]", "[link]"):
        assert mark in page


def test_scrub_leaves_plain_words_and_relative_doc_paths_alone():
    assert rh.scrub("Open docs/reference/plans.md and q_fit, 755 USD") == "Open docs/reference/plans.md and q_fit, 755 USD"


def transcript_record(**over):
    record = {"case": "E2E-WEBGOAL-01", "kind": "web_goal", "request": "Look up the date and add a goal.", "pinned_quest": "Sub-50 10K",
              "docs_in_view": [], "handoff": None, "writes": [{"tool": "create_quest_goal", "args": {"title": "Register by 12 Nov"}, "quest": "Sub-50 10K"}],
              "context_used": [{"kind": "quest", "quest": "Sub-50 10K", "text": "Sub-50 10K. Outcome: run a sub-50 10K", "why": "w", "inferred": True}],
              "steps": [{"n": 1, "action": "read", "why": "y", "did": [{"type": "web search", "query": "10K registration", "found": "closes 12 Nov"}]},
                        {"n": 2, "action": "tool", "why": "y", "did": [{"type": "write", "tool": "create_quest_goal", "args": {"title": "Register by 12 Nov"}}]},
                        {"n": 3, "action": "answer", "why": "done", "did": []}],
              "reply": "Closes 12 Nov. Added.", "verdict": {"ok": True, "note": "ok"}, "calls": 5}
    record.update(over)
    return record


def test_add_examples_builds_compact_scrubbed_examples_and_writes_atomically(tmp_path):
    import json
    from qar_overall import add_examples as ae
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "E2E-WEBGOAL-01.json").write_text(json.dumps(transcript_record(
        request=f"Mail jane@example.org the date from {HOME}/x/y", reply="See https://example.org/p")), encoding="utf-8")
    (tmp_path / "t" / "E2E-QUEST-24.json").write_text(json.dumps(transcript_record(
        case="E2E-QUEST-24", kind="quest_trap", writes=[], context_used=[], docs_in_view=["docs/guide/quests-and-plans.md"],
        side=None, rubric={"passed": 3, "total": 5}, reply="I am \u2014 not sure.")), encoding="utf-8")
    selection = [{"case": "E2E-QUEST-24", "shows": "not sure, no invention"}, {"case": "E2E-WEBGOAL-01", "shows": "search then goal"}]
    rows = ae.build(tmp_path / "t", selection)
    assert [r["id"] for r in rows] == ["E2E-QUEST-24", "E2E-WEBGOAL-01"]
    trap, goal = rows
    assert trap["group"] == "Questions about Quest" and trap["quality"] == {"passed": 3, "total": 5}
    assert trap["source"].startswith("checked, nothing found in:") and "\u2014" not in trap["result"]
    assert goal["group"] == "Multi-step operations" and goal["result_label"] == "Did" and goal["ok"] is True
    assert goal["steps"][0].startswith("1. searched the web for") and "[email]" in goal["request"] and "[path]" in goal["request"]
    content = tmp_path / "content.json"
    content.write_text(json.dumps({"title": "T", "decisions": []}), encoding="utf-8")
    assert ae.main(["--content", str(content), "--transcripts", str(tmp_path / "t"), "--select", str(_write(tmp_path / "sel.json", selection))]) == 0
    saved = json.loads(content.read_text())
    assert saved["title"] == "T" and [e["id"] for e in saved["examples"]] == ["E2E-QUEST-24", "E2E-WEBGOAL-01"]
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".tmp-")]


def _write(path, data):
    import json
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_add_examples_names_a_case_with_no_transcript_and_keeps_the_content_file(tmp_path):
    import json
    from qar_overall import add_examples as ae
    content = tmp_path / "content.json"
    content.write_text(json.dumps({"examples": [{"id": "keep"}]}), encoding="utf-8")
    try:
        ae.main(["--content", str(content), "--transcripts", str(tmp_path), "--select", str(_write(tmp_path / "s.json", [{"case": "E2E-NOPE-01"}]))])
        raise AssertionError("must stop")
    except SystemExit as stop:
        assert "E2E-NOPE-01" in str(stop)
    assert json.loads(content.read_text())["examples"] == [{"id": "keep"}]


def test_a_partial_run_row_is_labelled():
    data = fixture()
    data["e2e"]["sample"] = "only:E2E-QUEST-12"
    data["parts"][0]["sample"] = "only:E2E-QUEST-12"
    page = rh.render(data, CONTENT)
    assert page.count("partial run") >= 2
    assert "partial run" not in rh.render(fixture(), CONTENT)
