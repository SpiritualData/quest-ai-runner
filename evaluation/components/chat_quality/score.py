"""Score one chat-quality case from the record the live chat arm returns: pass means no hard pre-check failure and a
judge score at or above the pass threshold (``judge.case_passed``). An infrastructure error is a failure with its message.

The component's cases are every dataset case (``datasets/*.json``, the ``acceptance`` suite of 30 copies included) plus the two smoke cases in ``cases.json``,
plus the private poorly answered set when ``QAR_PRIVATE_POOR_ANSWERS_DATASET`` is present (``poor_answers.py``: a conditional case source, absent means no case);
``load_cases`` is the one place that says so, so the offline arm covers all of them and the live arm draws from them. When ``QAR_EVAL_OUTPUT_DIR`` is set
every scored case also appends its reply to the private output log (``output_log.py``)."""
import output_log
from chat_quality import poor_answers


def load_cases():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import runner
    return runner.load_cases("all") + runner.load_cases("acceptance") + runner.load_example_cases() + poor_answers.load_cases()


def route_summary(record):
    """The route and tool summary of a chat record: the planner actions and the tools the turns ran, when the record holds them."""
    turns = (record.get("evidence") or {}).get("turns") or []
    actions = [str(p.get("action")) for t in turns for p in t.get("plans") or [] if p.get("action")]
    tools = [str(e.get("tool")) for t in turns for e in t.get("execs") or [] if e.get("tool")]
    return "; ".join(part for part in (f"actions: {','.join(dict.fromkeys(actions))}" if actions else "",
                                       f"tools: {','.join(dict.fromkeys(tools))}" if tools else "") if part)


def input_of(case):
    return case.get("message") or " / ".join(case.get("messages") or [])


def score_case(fn, case):
    record = fn(case)
    row = scored(record)
    output_log.record(case.get("source") or case.get("dataset") or "chat_quality", case.get("id", ""), output_log.arm_of(fn), input_of(case),
                      ((record.get("evidence") or {}).get("reply") or record.get("reply") or ""), route_summary(record), record.get("score"),
                      row["ok"], row["note"] or record.get("error") or "", extra=record.get("log_extra"))
    return dict(row, source=case["source"]) if case.get("source") else row


def aggregate(rows):
    """The headline: the pass rate with the poorly answered source at its share of the number (``[balance.chat_quality]`` ``source_shares``)."""
    mine = [r for r in rows if r.get("source") == poor_answers.SOURCE]
    if not mine:
        return "poor_answers absent"
    other = [r for r in rows if r.get("source") != poor_answers.SOURCE]
    share = poor_answers.share()
    return (f"poor_answers {sum(bool(r['ok']) for r in mine)}/{len(mine)} others {sum(bool(r['ok']) for r in other)}/{len(other)} "
            f"share {share:g} weighted pass rate {poor_answers.rate_of_rows(rows, share):.3f}")


def scored(record):
    if record.get("error"):
        return dict(ok=False, got="error", note=str(record["error"])[:200])
    hard = (record.get("precheck") or {}).get("hard_failures") or []
    score = record.get("score")
    judged = record.get("judged") or {}
    summary = judged.get("summary") or (judged.get("verdict") or {}).get("summary") or ""
    note = "; ".join(hard) if hard else summary[:160]
    return dict(ok=bool(record.get("passed")), got="score " + ("n/a" if score is None else f"{score:.2f}"), note=note)
