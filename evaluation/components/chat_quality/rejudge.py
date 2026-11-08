"""Re-judge saved chat_quality records with no new chat turn: how much of a run-to-run difference is the judge's.

Each overlay record keeps the exact judge input (``judge_input``: the judged case, the evidence, the attempted changes, the precheck, the reads and the state check).
This scores each kept input again ``--times`` times with the verdict cache off, applies the same pass rule the run applied, and prints, per case and run, the
pass/fail of every judgment and the scores, then the number of (case, run) pairs whose verdict flipped across the re-judgments.

    python evaluation/components/chat_quality/rejudge.py --records DIR --runs 2 --times 3
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def kept_inputs(folder: Path, runs: int):
    """``{case_id: [record, ...]}``: the last ``runs`` records of each case that hold a judge input, oldest first."""
    out = {}
    for path in sorted(folder.glob("*.jsonl")):
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        kept = [r for r in rows if r.get("judge_input")]
        if kept:
            out[path.stem] = kept[-runs:]
    return out


def judged_once(record):
    """(passed, score) of one fresh judgment of the record's kept input, under the run's own pass rule."""
    import judge as J
    data = record["judge_input"]
    judged = J.judge(data["case"], data["evidence"], data["changes"], data["pre"], data["truth"], {}, None, production=True)
    passed = J.case_passed(data["case"], data["pre"], judged) and not data.get("unexpected")
    if data.get("state"):
        passed = passed and data["state"].get("ok", True)
    return passed, J.final_score(data["pre"], judged)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--records", required=True, help="the folder of per-case .jsonl records the overlay arm wrote")
    parser.add_argument("--runs", type=int, default=2, help="how many of each case's latest kept records to re-judge")
    parser.add_argument("--times", type=int, default=3, help="fresh judgments per record")
    parser.add_argument("--parallel", type=int, default=4)
    args = parser.parse_args()
    os.environ["QUAL_JUDGE_CACHE"] = "0"
    cases = kept_inputs(Path(args.records), args.runs)
    jobs = [(cid, i, rec) for cid, recs in cases.items() for i, rec in enumerate(recs) for _ in range(args.times)]
    with ThreadPoolExecutor(args.parallel) as pool:
        results = list(pool.map(lambda job: (job[0], job[1], job[2].get("passed"), judged_once(job[2])), jobs))
    table = {}
    for cid, run, original, (passed, score) in results:
        table.setdefault((cid, run), {"original": original, "verdicts": [], "scores": []})
        table[(cid, run)]["verdicts"].append(passed)
        table[(cid, run)]["scores"].append(score)
    flipped = 0
    for (cid, run), row in sorted(table.items()):
        verdicts = row["verdicts"] + [row["original"]]
        unstable = len(set(verdicts)) > 1
        flipped += unstable
        print(f"{cid:8} run {run + 1}: original {'PASS' if row['original'] else 'FAIL'} | re-judged {' '.join('PASS' if v else 'FAIL' for v in row['verdicts'])} "
              f"| scores {row['scores']}{'  <- judge flips' if unstable else ''}")
    print(f"rejudge: {flipped} of {len(table)} (case, run) verdicts flipped across {args.times} fresh judgments plus the original")


if __name__ == "__main__":
    main()
