"""Re-record every owner arm of the prompts in ``cases.json`` and then the ablation arm, in order, in one command.

``python3 evaluation/components/justified_model_spend/record_owner_arms.py [--plan] [--api --api-usd-cap 0.05] [--tier offline|subscription|api]
[--only component/impl ...] [--in-place [--wait-clean SECONDS | --allow-dirty]]``

Why one command: an owner row goes stale the moment code under ``code_paths`` changes after its commit, so ``QME8`` (the share of
average spend that is justified) reads 0.0 until every owner arm and the ablation arm are recorded again at one clean commit. This
script does that pass, in the order of ``owner_arms.json``: the no-call floors, the subscription arms (`claude -p`, no API dollars), the
paid API arms (only with ``--api``, inside ``--api-usd-cap`` dollars), then ``esoi run justified_model_spend --impl spec_registry_ablation
--record``. Every command runs under ``flock /tmp/qar-heavy.lock`` (one heavy job at a time on this machine).

Each arm goes through ``measure.py`` (which records the row and the per-call trace together). A paid arm that made no model call
records nothing and stops the pass (``--min-calls``), so a 0-call run never becomes a row. A pass stops at the first failing arm.
By default the pass runs in a detached snapshot of HEAD (a temporary ``git worktree``), so rows carry the commit with no ``-dirty`` however
many edits other agents have waiting in the shared working copy; after every command the new results rows and traces are copied back
here and the snapshot is removed at the end. ``--in-place`` runs in this working copy instead and refuses a tree with uncommitted measured
code (a row recorded on a dirty tree is stale at birth) unless ``--allow-dirty``. ``--plan`` prints the commands and runs nothing. Tracker rebuilds are separate (``esoi tracker build``)."""
import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
LOCK = "/tmp/qar-heavy.lock"
TIER_ORDER = ("offline", "subscription", "api")
PYTHON = str(REPO / ".venv" / "bin" / "python3") if (REPO / ".venv" / "bin" / "python3").exists() else sys.executable   # the repo's own interpreter
SIBLING_ESOI = REPO.parent / "evolvable-structure" / "implementations" / "python"


def load_manifest(path=HERE / "owner_arms.json"):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def select_arms(manifest, tiers=None, only=None, api=False):
    """The arms to run, in manifest order. API arms are left out unless ``api``; ``tiers`` and ``only`` (``component/impl``) narrow it."""
    chosen = []
    for arm in manifest["arms"]:
        if arm["tier"] == "api" and not api:
            continue
        if tiers and arm["tier"] not in tiers:
            continue
        if only and f"{arm['component']}/{arm['impl']}" not in only:
            continue
        chosen.append(arm)
    return chosen


def arm_command(arm):
    cmd = ["flock", LOCK, PYTHON, str(HERE / "measure.py"), arm["component"], arm["impl"], "--record"]
    if arm.get("max_calls"):
        cmd += ["--max-calls", str(arm["max_calls"])]
    if arm.get("min_calls"):
        cmd += ["--min-calls", str(arm["min_calls"])]
    return cmd + list(arm.get("args", []))


def ablation_command(manifest):
    ab = manifest["ablation"]
    return ["flock", LOCK, PYTHON, "-m", "esoi", "run", ab["component"], "--impl", ab["impl"], "--record"]


def build_plan(manifest, tiers=None, only=None, api=False):
    """``[(label, command)]``: the selected arms in order, then the ablation arm (unless ``only`` or ``tiers`` narrows the pass)."""
    plan = [(f"{a['component']}/{a['impl']}", arm_command(a)) for a in select_arms(manifest, tiers, only, api)]
    if not only and not tiers:
        ab = manifest["ablation"]
        plan.append((f"{ab['component']}/{ab['impl']}", ablation_command(manifest)))
    return plan


def api_estimate(manifest, api=False, tiers=None, only=None):
    return sum(a.get("est_usd", 0.0) for a in select_arms(manifest, tiers, only, api) if a["tier"] == "api")


def dirty_paths(run=subprocess.run, cwd=REPO):
    """Uncommitted edits to tracked measured code (``quest_ai_runner`` and ``evaluation/components``; prose and the results log excluded:
    recording writes it). Untracked files are not counted: a row is stale when committed code changed, not when a new file waits."""
    out = run(["git", "-C", str(cwd), "status", "--porcelain", "-uno", "--", "quest_ai_runner", "evaluation/components"],
              capture_output=True, text=True).stdout.splitlines()
    return [line[3:] for line in out if not line.endswith(".md") and "__pycache__" not in line
            and not line.endswith("evaluation/components/results.csv") and not line[3:].startswith("evaluation/traces")]


def wait_until_clean(run, seconds, sleep=time.sleep, interval=30):
    """The uncommitted measured code, re-checked every ``interval`` seconds for up to ``seconds`` (another agent may be mid-edit; any
    change to the measured code makes the row's ``git_sha`` carry ``-dirty``, so a pass cannot record until the tree is clean)."""
    waited = 0
    found = dirty_paths(run)
    while found and waited < seconds:
        sleep(interval)
        waited += interval
        found = dirty_paths(run)
    return found


def last_cost(component, impl, results=REPO / "evaluation" / "components" / "results.csv"):
    """Dollars of the newest recorded row of an arm (0.0 when none or unreadable)."""
    try:
        rows = [r for r in csv.DictReader(open(results, newline="", encoding="utf-8")) if r["component"] == component and r["impl"] == impl]
        return float(rows[-1]["cost_usd"] or 0.0) if rows else 0.0
    except (OSError, ValueError, KeyError):
        return 0.0


def rebase(cmd, root):
    """``cmd`` with every path under the repository moved under ``root`` (the snapshot a pass runs in)."""
    return [a if a == PYTHON else a.replace(str(REPO), str(root)) for a in cmd]


def add_snapshot(run=subprocess.run):
    """A detached checkout of HEAD in a temporary folder: clean by construction, so rows recorded there carry the commit with no
    ``-dirty`` however many other edits are waiting in the shared working copy. Returns its path."""
    root = tempfile.mkdtemp(prefix="qar-jms-")
    done = run(["git", "-C", str(REPO), "worktree", "add", "--detach", root, "HEAD"], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"could not check out HEAD: {done.stderr}")
    return root


def remove_snapshot(root, run=subprocess.run):
    run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(root)], capture_output=True, text=True)


class Sync:
    """Copies what recording wrote in the snapshot back into the repository: new rows of the results log (appended, once each) and the
    per-call traces. ``copy`` after every command, so a stopped pass still leaves the rows it finished."""

    def __init__(self, root):
        self.root = Path(root)
        self.results = Path("evaluation/components/results.csv")
        self.traces = Path("evaluation/traces/justified_model_spend")
        self.seen = len(self.lines(self.root / self.results))

    @staticmethod
    def lines(path):
        try:
            return path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            return []

    def copy(self):
        fresh = self.lines(self.root / self.results)[self.seen:]
        if fresh:
            with open(REPO / self.results, "a", encoding="utf-8", newline="") as handle:
                handle.writelines(line if line.endswith("\n") else line + "\n" for line in fresh)
            self.seen += len(fresh)
        source = self.root / self.traces
        if source.is_dir():
            (REPO / self.traces).mkdir(parents=True, exist_ok=True)
            for file in source.glob("*.json"):
                target = REPO / self.traces / file.name
                if not target.exists() or target.read_bytes() != file.read_bytes():
                    target.write_bytes(file.read_bytes())
        return len(fresh)


def run_pass(manifest, plan, *, api_cap, run=subprocess.run, cost_of=last_cost, say=print, root=REPO, after=None):
    """Run ``plan`` in order inside ``root``; returns 0 or the failing command's exit code. ``after()`` runs after every command (the sync
    back from a snapshot). Stops when a paid API arm takes the spend over ``api_cap``."""
    spent = 0.0
    paid = {f"{a['component']}/{a['impl']}": a["tier"] == "api" for a in manifest["arms"]}
    for label, cmd in plan:
        say(f"== {label}")
        done = run(rebase(cmd, root), env=env_with_esoi(root), cwd=str(root))
        if after:
            after()
        if done.returncode != 0:
            say(f"STOPPED: {label} exited {done.returncode}; nothing after it was run")
            return done.returncode or 1
        if paid.get(label):
            comp, impl = label.split("/")
            spent += cost_of(comp, impl)
            if spent > api_cap:
                say(f"STOPPED: API spend ${spent:.4f} is over the cap ${api_cap}")
                return 2
    say(f"done: {len(plan)} commands, API spend ${spent:.4f}")
    return 0


def env_with_esoi(root=REPO):
    env = dict(os.environ)
    parts = [str(Path(root) / "evaluation" / "components"), str(root)]
    if SIBLING_ESOI.is_dir():
        parts.append(str(SIBLING_ESOI))
    env["PYTHONPATH"] = os.pathsep.join(parts + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return env


def main(argv=None, run=subprocess.run):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--plan", action="store_true", help="print the commands and run nothing")
    ap.add_argument("--api", action="store_true", help="also run the paid API arms")
    ap.add_argument("--api-usd-cap", type=float, default=0.05, help="stop when the API arms have cost more than this (dollars)")
    ap.add_argument("--tier", action="append", choices=TIER_ORDER, help="only these tiers (the ablation arm is then skipped)")
    ap.add_argument("--only", action="append", metavar="COMPONENT/IMPL", help="only these arms (the ablation arm is then skipped)")
    ap.add_argument("--in-place", action="store_true", help="run in this working copy instead of a clean snapshot of HEAD (needs a clean tree)")
    ap.add_argument("--wait-clean", type=int, default=0, metavar="SECONDS", help="--in-place: wait this long for other edits to be committed")
    ap.add_argument("--allow-dirty", action="store_true", help="--in-place: run on a tree with uncommitted measured code")
    args = ap.parse_args(argv)
    manifest = load_manifest()
    plan = build_plan(manifest, args.tier, args.only, args.api)
    estimate = api_estimate(manifest, args.api, args.tier, args.only)
    if estimate > args.api_usd_cap:
        print(f"refused: the API arms are estimated at ${estimate:.4f}, over --api-usd-cap {args.api_usd_cap}")
        return 2
    for label, cmd in plan:
        print(f"{label}: {' '.join(cmd)}")
    if args.plan:
        print(f"API estimate ${estimate:.4f}; cap ${args.api_usd_cap}")
        return 0
    if not args.in_place:
        root = add_snapshot(run)
        say_root = f"snapshot of HEAD at {root}"
        print(say_root)
        sync = Sync(root)
        try:
            return run_pass(manifest, plan, api_cap=args.api_usd_cap, run=run, root=root, after=sync.copy)
        finally:
            sync.copy()
            remove_snapshot(root, run)
    dirty = [] if args.allow_dirty else wait_until_clean(run, args.wait_clean)
    if dirty:
        print("refused: uncommitted changes under the measured code would make every recorded row stale at birth:\n  " + "\n  ".join(dirty[:20]))
        return 3
    return run_pass(manifest, plan, api_cap=args.api_usd_cap, run=run)


if __name__ == "__main__":
    sys.exit(main())
