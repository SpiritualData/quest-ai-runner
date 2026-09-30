"""Classification eval of the deep-run LIVENESS REVIEW with the REAL reviewer model.

The review (``DEEP_REVIEW_PROMPT`` via ``SubprocessGoalRunner._call_reviewer_cli``, the same
one-shot ``claude -p --model haiku`` call production makes every 5 minutes) answers one binary
question about a run's live log tail: stop=true or stop=false. This feeds 40 hand-labeled cases
through the production prompt, parser and CLI call and requires 100% agreement.

Labels follow the rule the prompt encodes: stop only for idle > 5 minutes with no legitimate
reason, out of tokens / usage limit, blocked on a human decision, or stuck repeating a failing
action; keep running for progress and for legitimately slow work (monitoring, builds, tests).
Wrong stops lose work that was about to land, so near-miss cases lean on the keep side only where
the log itself shows the work is alive.

Run:  python evaluation/deep_review_eval.py [--runs N]   (N repeats each case; default 1)
Exit code is 0 only when every case in every run is classified correctly.
"""
import argparse
import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO)

from quest_ai_runner.core.goal_runner import (  # noqa: E402
    DEEP_REVIEW_PROMPT, SubprocessConfig, SubprocessGoalRunner, parse_deep_review)

# (name, expected_stop, elapsed_seconds, idle_seconds, log_tail)
CASES = [
    # ---- keep running: active progress -------------------------------------------------------
    ("editing files, fresh log", False, 900, 4,
     "- Read: src/api/users.py\n- Edit: src/api/users.py\n- $ pytest tests/test_users.py -q\n- Tests pass, now updating the docs."),
    ("about to commit", False, 3500, 6,
     "- $ pytest -q\n- 212 passed\n- $ git add -A\n- Everything is green. Committing the change now."),
    ("just started", False, 40, 2,
     "- Read: CLAUDE.md\n- Read: src/app.py"),
    ("long task, steady tool use", False, 7200, 12,
     "- Read: a.py\n- Edit: a.py\n- Read: b.py\n- Edit: b.py\n- $ ruff check .\n- Fixing the 3 lint errors."),
    ("subagents working, recent activity", False, 1800, 20,
     "- Launched 3 research subagents in parallel.\n- Subagent 2 returned its findings.\n- Waiting on the remaining two."),
    ("writing final summary", False, 2600, 3,
     "- $ git commit -m 'Add retry to sync'\n- Committed abc123.\n- Drafting the final report for the user."),
    ("fixed a failure then moved on", False, 1500, 9,
     "- $ pytest -q\n- 1 failed: test_parse\n- Edit: parser.py\n- $ pytest -q\n- 40 passed\n- Moving to the next subtask."),
    ("reading many files, fresh", False, 600, 15,
     "- Read: docs/a.md\n- Read: docs/b.md\n- Read: docs/c.md\n- Grep: 'retry'\n- Read: src/retry.py"),
    # ---- keep running: legitimately slow work ------------------------------------------------
    ("monitoring a deploy, sleeping", False, 4000, 120,
     "- $ gh run watch 991822\n- Workflow 'deploy' is in progress (step 4 of 7)\n- Still in progress; I will keep polling until it finishes as the task asks me to confirm the deploy."),
    ("monitoring with periodic polling", False, 5400, 200,
     "- Task: watch the nightly import until it completes and report the row count.\n- $ sleep 180; psql -c 'select count(*) from imported'\n- 41,200 rows so far, import still running."),
    ("long test run in progress", False, 3000, 150,
     "- $ pytest tests/integration -x\n- Running the full integration suite, which normally takes about 20 minutes. 620 of ~1400 tests complete."),
    ("docker build in progress", False, 2000, 240,
     "- $ docker build -t quest-backend .\n- Step 9/14 : RUN pip install -r requirements.txt\n- The image build is downloading dependencies."),
    ("waiting for model training", False, 6000, 280,
     "- Task: train the classifier and report accuracy.\n- $ python train.py --epochs 50\n- epoch 31/50 loss 0.21 (each epoch takes about 4 minutes)"),
    ("monitoring task, quiet but by design", False, 9000, 270,
     "- Task: check the queue every few minutes for one hour and summarize anything unusual.\n- Checked at 14:05: 0 stuck items.\n- Sleeping until the next check at 14:10."),
    ("large data download", False, 1200, 100,
     "- $ curl -O https://example.org/dataset.tar.gz\n- Downloading 4.2 GB, 61% done."),
    ("waiting on CI with explicit task", False, 2400, 220,
     "- Task: open the PR and wait for CI, then merge if green.\n- PR opened.\n- $ gh pr checks 88 --watch\n- 3 of 5 checks passed, 2 pending."),
    # ---- keep running: transient hiccups that recover ---------------------------------------
    ("one failed command then retry works", False, 1300, 8,
     "- $ npm test\n- Error: port 3000 in use\n- $ kill 4411\n- $ npm test\n- 58 tests passed."),
    ("idle short, under threshold", False, 800, 45,
     "- Read: notes.md\n- Thinking through the migration order before editing."),
    ("idle 3 minutes mid-task", False, 1900, 180,
     "- $ pytest tests/test_big.py\n- collecting 900 tests, this suite has a slow start."),
    ("progress just after long pause, log fresh", False, 5000, 10,
     "- $ make bundle\n- Bundle finished after 11 minutes.\n- $ ls dist/\n- Copying the bundle into place."),
    # ---- stop: idle too long -----------------------------------------------------------------
    ("idle 20 minutes, nothing pending", True, 2500, 1200,
     "- Edit: src/api.py\n- Edit: src/api.py"),
    ("idle 15 minutes after a read", True, 1800, 900,
     "- Read: README.md"),
    ("idle 40 minutes after partial answer", True, 4000, 2400,
     "- Let me look at the failing test.\n- Read: tests/test_sync.py"),
    ("idle 10 minutes, no command running", True, 3700, 600,
     "- $ git status\n- On branch september, nothing to commit\n- I'll now plan the next step."),
    ("idle over an hour", True, 8000, 4000,
     "- Read: config.yaml"),
    ("idle 7 minutes after error", True, 1000, 420,
     "- $ python run.py\n- Traceback (most recent call last): KeyError: 'id'"),
    # ---- stop: out of tokens / limits -------------------------------------------------------
    ("usage limit message", True, 1500, 30,
     "- Edit: app.py\n- You've hit your usage limit. Your limit will reset at 5pm."),
    ("out of credits", True, 900, 20,
     "- $ pytest -q\n- Credit balance is too low to continue. Please add credits."),
    ("rate limit exhausted repeated", True, 1100, 60,
     "- API Error: 429 rate_limit_error. Retrying in 30s\n- API Error: 429 rate_limit_error. Retrying in 30s\n- API Error: 429 rate_limit_error. You have exceeded your quota."),
    ("token budget exhausted", True, 2000, 25,
     "- Read: big.log\n- Error: context window and token budget exhausted; the session cannot continue."),
    # ---- stop: blocked on a human decision ---------------------------------------------------
    ("asks user which option", True, 700, 500,
     "- I found two conflicting configs. I cannot proceed without a decision.\n- Which one should I use, prod.yaml or staging.yaml? Waiting for your answer."),
    ("needs credentials it cannot get", True, 950, 400,
     "- $ gh auth status\n- Not logged in.\n- I am blocked: I need a token from the user to push, and there is nobody to provide it. Waiting."),
    ("requesting approval for irreversible act", True, 1300, 650,
     "- Ready to drop the production table users_old.\n- This is irreversible. Please confirm before I continue. QAR-ESCALATED: dec_8842"),
    ("blocked on missing task info", True, 600, 480,
     "- The task says to update 'the usual report' but no report is named anywhere.\n- I cannot continue until someone tells me which report. Stopping here and waiting."),
    ("permission denied, cannot proceed", True, 800, 540,
     "- $ cat /etc/secrets/key\n- Permission denied\n- $ sudo cat /etc/secrets/key\n- sudo: a password is required\n- I am blocked without that access and cannot finish the task."),
    # ---- stop: stuck repeating the same failing action --------------------------------------
    ("same failing command loop", True, 2200, 5,
     "- $ pytest tests/test_x.py\n- FAILED test_x: ImportError: no module named foo\n- $ pytest tests/test_x.py\n- FAILED test_x: ImportError: no module named foo\n- $ pytest tests/test_x.py\n- FAILED test_x: ImportError: no module named foo\n- $ pytest tests/test_x.py\n- FAILED test_x: ImportError: no module named foo"),
    ("same edit undone repeatedly", True, 3100, 4,
     "- Edit: a.py (add import)\n- $ ruff check a.py\n- error: unused import\n- Edit: a.py (remove import)\n- $ python a.py\n- NameError: name 'json' is not defined\n- Edit: a.py (add import)\n- $ ruff check a.py\n- error: unused import\n- Edit: a.py (remove import)\n- $ python a.py\n- NameError: name 'json' is not defined"),
    ("curl timing out over and over", True, 1800, 6,
     "- $ curl https://api.internal/health\n- curl: (28) Connection timed out\n- $ curl https://api.internal/health\n- curl: (28) Connection timed out\n- $ curl https://api.internal/health\n- curl: (28) Connection timed out\n- $ curl https://api.internal/health\n- curl: (28) Connection timed out\n- $ curl https://api.internal/health\n- curl: (28) Connection timed out"),
    ("empty log, long idle", True, 3000, 1500,
     "(the session log is empty or unreadable)"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()
    assert len(CASES) >= 30, "the eval must hold at least 30 cases"

    runner = SubprocessGoalRunner(SubprocessConfig(working_dir="/tmp", claude_path="claude"))
    wrong = []
    total = 0
    for run in range(args.runs):
        for name, want, elapsed, idle, tail in CASES:
            prompt = DEEP_REVIEW_PROMPT.format(elapsed=elapsed, idle=idle, tail=tail)
            got, reason = None, ""
            try:
                verdict = parse_deep_review(runner._call_reviewer_cli(prompt))
                if verdict is not None:
                    got, reason = verdict
            except Exception as e:  # noqa: BLE001
                reason = f"call failed: {e}"
            total += 1
            ok = got is want
            print(f"[{'PASS' if ok else 'FAIL'}] run{run + 1} {name}: want stop={want} got={got} ({reason})")
            if not ok:
                wrong.append((run + 1, name))
    print(f"\n{total - len(wrong)}/{total} correct")
    for r, n in wrong:
        print(f"  WRONG run{r}: {n}")
    return 0 if not wrong else 1


if __name__ == "__main__":
    sys.exit(main())
