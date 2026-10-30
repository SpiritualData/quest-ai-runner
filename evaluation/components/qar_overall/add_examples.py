"""Write the ``examples`` key of the private handoff content file from saved end_to_end transcripts.

The end_to_end live arm saves one JSON per case when ``QAR_E2E_TRANSCRIPTS=<directory>`` is set (scratch output, never committed).
This script reads that directory plus a small selection file and turns each chosen transcript into the compact example the page
shows (``render_handoff.section_examples``):

    python evaluation/components/qar_overall/add_examples.py --content <content.json> --transcripts <dir> --select <selection.json>

The selection is a JSON list of ``{"case": "E2E-GOAL-01", "shows": "one line on what this example shows", "group": optional}``;
the page group defaults from the case kind. The content file is replaced whole through a temp file and ``os.replace``, so a reader
never sees half of it. Every text that reaches the page is scrubbed first: an email, a link, an absolute path and an id-looking
string become ``[email]``, ``[link]``, ``[path]`` and ``[id]``, and an em dash becomes a comma. Nothing here makes a model call."""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from qar_overall import render_handoff as rh  # noqa: E402

GROUP_OF_KIND = {
    "goal_write": "Adding a goal", "note_write": "Adding a goal", "implicit_quest": "Using context the person did not mention",
    "own_data": "Using context the person did not mention", "multistep": "Multi-step operations", "web_goal": "Multi-step operations",
    "web": "Multi-step operations", "sequential": "Multi-step operations", "hand_off": "Deep run hand-off", "refusal": "Refusals and consent",
    "science": "Science and evidence",
}
SETTINGS_KINDS = {"quest_settings"}
QUEST_KINDS = {"quest_consult", "quest_clarify", "quest_why", "quest_feature", "quest_mentor", "quest_trap", "quest_ai", "docs_lookup"}
CUT = 360


def safe(text, limit=CUT):
    text = rh.scrub(rh.clean(" ".join(str(text or "").split())))
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def args_text(args):
    return ", ".join(f"{k}={safe(v, 70)!r}" if isinstance(v, str) else f"{k}={v}" for k, v in list((args or {}).items())[:3])


def step_text(step):
    parts = []
    for did in step.get("did", []):
        if did["type"] == "web search":
            parts.append(f"searched the web for {safe(did['query'], 80)!r}")
        elif did["type"] == "read":
            parts.append(f"read {did['tool']}")
        else:
            parts.append(f"wrote {did['tool']}({args_text(did['args'])})")
    if step.get("handoff"):
        parts.append("handed off a deep run: " + safe(step["handoff"].get("goal"), 120))
    if step.get("question"):
        parts.append("asked: " + safe(step["question"], 120))
    if not parts:
        parts.append({"answer": "replied", "confirm": "asked for a yes", "clarify": "asked a question"}.get(step.get("action"), step.get("action", "step")))
    return safe(f"{step.get('n', '')}. " + "; ".join(parts), 220)


def context_items(record):
    out = []
    for c in record.get("context_used", []):
        if c.get("source"):   # a documentation page is cited by name, its text stays out of the page
            out.append({"label": safe(f"{c['source']} ({c['title']})" if c.get("title") else c["source"], 110)})
            continue
        label = c.get("source") or (f"{c.get('kind', 'record')} on {c['quest']}" if c.get("quest") else c.get("kind", "record"))
        out.append({"label": safe(label, 80), "text": safe(c.get("text"), 140)})
    return out


def group_of(record, chosen):
    if chosen.get("group"):
        return chosen["group"]
    kind = record.get("kind", "")
    if kind in SETTINGS_KINDS:
        return "Settings and account"
    return "Questions about Quest" if kind in QUEST_KINDS else GROUP_OF_KIND.get(kind, "Other")


def source_of(record):
    used = list(dict.fromkeys(c["source"] for c in record.get("context_used", []) if c.get("source")))
    if used:
        return ", ".join(used)
    seen = record.get("docs_in_view") or []
    return ("checked, nothing found in: " + ", ".join(seen)) if seen else ""


def example_of(record, chosen):
    writes = record.get("writes") or []
    handoff = record.get("handoff")
    if writes:
        label = "Did"
        result = "; ".join(f"{w['tool']}({args_text(w['args'])})" + (f" on {safe(w['quest'], 60)}" if w.get("quest") else "") for w in writes)
    elif handoff:
        label, result = "Handed off", f"goal {safe(handoff.get('goal'), 140)}; brief {safe(handoff.get('brief'), 160)}"
    else:
        label, result = "Reply", safe(record.get("reply_full") or record.get("reply"))
    example = dict(id=record["case"], group=group_of(record, chosen), shows=safe(chosen.get("shows"), 160), request=safe(record.get("request"), 300),
                   context=context_items(record), steps=[step_text(s) for s in record.get("steps", [])][:6], result=result, result_label=label,
                   ok=bool(record.get("verdict", {}).get("ok")), source=safe(source_of(record), 200))
    if record.get("side") or chosen.get("side"):
        example["side"] = safe(chosen.get("side") or record["side"], 20)
    quality = record.get("rubric")
    if quality and quality.get("total"):
        example["quality"] = {"passed": int(quality["passed"]), "total": int(quality["total"])}
    return {k: v for k, v in example.items() if v not in ("", [], None)}


def build(directory, selection, case_info=None):
    """The examples list for ``selection``, in its order. Raises SystemExit naming every case with no transcript."""
    rows, missing = [], []
    for chosen in selection:
        path = Path(directory) / f"{chosen['case']}.json"
        if not path.is_file():
            missing.append(chosen["case"])
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if case_info and chosen["case"] in case_info:
            record.setdefault("side", case_info[chosen["case"]].get("side"))
        rows.append(example_of(record, chosen))
    if missing:
        sys.exit(f"add_examples: no transcript for {', '.join(missing)} in {directory}")
    return rows


def write_atomic(path, data):
    """Replace ``path`` with ``data`` as JSON through a temp file in the same folder."""
    path = Path(path)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, ensure_ascii=False)
            handle.write("\n")
        os.replace(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--content", required=True, help="the private content JSON to update")
    ap.add_argument("--transcripts", required=True, help="the directory the live arm saved transcripts in")
    ap.add_argument("--select", required=True, help="selection JSON: a list of {case, shows, group?}")
    ap.add_argument("--dry-run", action="store_true", help="print the examples and write nothing")
    args = ap.parse_args(argv)
    selection = json.loads(Path(args.select).read_text(encoding="utf-8"))
    if isinstance(selection, dict):
        selection = selection.get("examples", [])
    import common
    cases = {c["id"]: c for c in common.load_cases("end_to_end")}
    examples = build(args.transcripts, selection, cases)
    if args.dry_run:
        print(json.dumps(examples, indent=1, ensure_ascii=False))
        return 0
    content_path = Path(args.content).resolve()
    content = json.loads(content_path.read_text(encoding="utf-8")) if content_path.is_file() else {}
    content["examples"] = examples
    write_atomic(content_path, content)
    print(f"wrote {len(examples)} examples to the content file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
