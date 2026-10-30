"""The quality rubric of an end-to-end case: 3 to 5 yes/no items a judge checks on the reply, beyond what ``score.py`` can see.

``score.py`` checks required facts, forbidden inventions, a question when one is needed and the dash and price rules for free.
A reply can pass all of that and still be a feature dump, or speak to nobody in particular. The rubric asks what a person would:
does it name THEIR goal, does it say why and not only what, does it ask before assuming, is it plain. It is judged by a model, so it
is NEVER part of ``score_case``; it runs only when asked, on saved transcripts, on the Claude Code subscription (``claude -p``):

    python evaluation/components/end_to_end/rubric.py <transcript directory> [--max-calls N]

and writes the result into each transcript as ``rubric`` (``{"passed", "total", "items": [{"item", "yes", "why"}]}``).
``prompt`` and ``parse`` are pure and tested offline with canned replies; only ``grade`` makes a call."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LOW, HIGH = 3, 5


def items(case):
    """The case's rubric items, checked to be 3 to 5 non-empty strings without an em dash."""
    got = case.get("quality_rubric") or []
    if not (LOW <= len(got) <= HIGH) or not all(isinstance(x, str) and x.strip() and not re.search(r"[\u2014\u2013]", x) for x in got):
        raise ValueError(f"{case.get('id')}: quality_rubric must be {LOW} to {HIGH} plain yes/no items")
    return list(got)


def prompt(case, reply, docs=None):
    """The grader prompt: the request, the facts the reply may use, the reply and one question per item."""
    facts = "\n\n".join(f"[{d['source']}]\n{d['text']}" for d in docs or [])
    lines = "\n".join(f"{i}. {item}" for i, item in enumerate(items(case), 1))
    return (f"A person sent this message to an AI assistant:\n\n{case['message']}\n\n"
            + (f"The only facts the assistant could use (documentation excerpts):\n\n{facts}\n\n" if facts else "")
            + f"The assistant replied:\n\n{reply}\n\n"
            f"Answer each question about the reply with yes or no, judging only the reply against the message and the facts above. "
            f"A question that does not apply because the case needs no such thing is a yes only when the reply does not do the opposite.\n\n{lines}\n\n"
            'Reply with one JSON object: {"items": [{"n": 1, "yes": true, "why": "one short clause"}, ...]}, one entry per question, in order.')


def parse(got, case):
    """The graded result from the grader's JSON object; an object that cannot be read counts every item as not passed."""
    wanted = items(case)
    rows = {}
    for entry in (got or {}).get("items", []) if isinstance(got, dict) else []:
        if isinstance(entry, dict) and isinstance(entry.get("n"), int):
            rows[entry["n"]] = entry
    out = [{"item": text, "yes": bool(rows.get(i, {}).get("yes")) and "yes" in rows.get(i, {}), "why": str(rows.get(i, {}).get("why", ""))[:160]}
           for i, text in enumerate(wanted, 1)]
    error = None if rows else str((got or {}).get("error", "no readable grader answer"))[:160]
    return dict(passed=sum(1 for x in out if x["yes"]), total=len(out), items=out, **({"error": error} if error else {}))


def grade(case, reply, provider, docs=None):
    """One grader call (``claude_judge.ask_json``) and its parsed result."""
    import claude_judge
    return parse(claude_judge.ask_json(provider, prompt(case, reply, docs)), case)


def grade_directory(directory, cases, provider, docs_for):
    """Grade every transcript in ``directory`` whose case has a rubric; returns {case id: result}. Skips one already graded."""
    results = {}
    for path in sorted(Path(directory).glob("E2E-*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        case = cases.get(record.get("case"))
        if not case or not case.get("quality_rubric") or "rubric" in record:
            continue
        record["rubric"] = results[record["case"]] = grade(case, record.get("reply_full") or record.get("reply", ""), provider, docs_for(case))
        path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return results


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("directory")
    ap.add_argument("--max-calls", type=int, required=True, help="the model-call budget (one call per case)")
    args = ap.parse_args(argv)
    import common
    import claude_judge
    from end_to_end import drive
    cases = {c["id"]: c for c in common.load_cases("end_to_end")}
    meter = common.CallMeter(max_calls=args.max_calls)
    provider = claude_judge.cli_provider(meter, claude_judge.JUDGE_MODEL)
    done = grade_directory(args.directory, cases, provider, lambda c: drive.new_world(c).docs)
    for cid, r in done.items():
        print(f"[{cid}] rubric {r['passed']}/{r['total']}" + (f" ({r['error']})" if r.get("error") else ""))
    print(f"{len(done)} graded, {meter.calls} model calls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
