"""PRODUCTION CHAT, OVERLAY REPLAY (live model, nothing written): each poor_answers case is one Quest AI chat turn run exactly as the production chat route runs it, on
production data, with THIS checkout's code laid over the deployed image, so a fix is proved before it is released.

The turn runs on the production server inside the backend container (``quest-backend/scripts/checks/quest_chat_replay.py``): the same builder, tools, planner, reach judge and
streaming function the route calls, as the case's caller, in a fresh in-memory conversation pinned to the lane's quest (or to the quest the case names, or to none), with the case's prior turns (the recorded
replies too) as the conversation's earlier messages. Every database and vector write is BLOCKED and recorded; the recorded attempts are the turn's "changes", checked against the
case's ``effect`` exactly as ``prod_quest_chat`` checks the quest's before and after (an attempted write the case does not allow is an UNEXPECTED WRITE and fails the case). The reads come
from the tools' own results (the data, not summaries). The judge is the same ``chat_quality`` judge (``claude -p``).

Overlay: before the first case this arm ships the backend files that differ from ``QAR_OVERLAY_BASE_REF`` (the deployed release commit) and the whole quest-ai-runner
package of this checkout into the container's ``/tmp/qoverlay`` (nothing is installed; a replay process puts them first on its path). Differs from the real route: no HTTP request and
no SSE transport, no parked-decision shortcut, and the reply and the user message are not stored. The model calls are real production calls.

Environment: ``QAR_PRIVATE_POOR_ANSWERS_DATASET`` (approved ids and texts), ``QAR_PROD_CASE_RULES_FILE``, ``QAR_OVERLAY_SSH`` (user@host), ``QAR_OVERLAY_REMOTE_DIR`` (the compose directory), ``QAR_OVERLAY_BASE_REF`` (the deployed backend commit), ``QAR_OVERLAY_USER`` (the caller's user id),
``QAR_PROD_QUEST_ID``, ``QAR_PROD_TEAM_ID``, ``QAR_OVERLAY_BACKEND`` (optional: the quest-backend checkout).
PAID in production model calls; the judge is ``claude -p``."""
PAID = True
ALTERNATIVE = True
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import prodchat as PC  # noqa: E402

QAR_ROOT = Path(__file__).resolve().parents[4]

#: The collection (word) a blocked write reached -> the fingerprint category the case's ``effect`` speaks of. Anything else is reported as an other write and not counted.
CATEGORY_WORDS = (("workflow", "workflows"), ("assistant_task", "tasks"), ("decision", "decisions"), ("spec", "spec_records"), ("note", "notes"), ("goals", "goals"))
IGNORED_WORDS = ("step_run", "step_dispatch", "step_snooze", "step_notice")
KEPT_MARKER = "Kept by Quest AI:"


def configured():
    return bool(os.environ.get("QAR_PRIVATE_POOR_ANSWERS_DATASET"))


def need(name):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"chat_quality: {name} is not set (see the prod_chat_overlay docstring)")
    return value


def ssh_target():
    return need("QAR_OVERLAY_SSH"), need("QAR_OVERLAY_REMOTE_DIR")


def remote_exec(command, stdin=None, data=None, timeout=900):
    host, directory = ssh_target()
    return subprocess.run(["ssh", "-o", "BatchMode=yes", host, f"cd {directory} && docker compose exec -T quest-backend {command}"],
                          stdin=stdin, input=data, capture_output=True, timeout=timeout)


def backend_checkout():
    return Path(os.environ.get("QAR_OVERLAY_BACKEND") or QAR_ROOT.parent / "quest-backend")


def changed_backend_files(checkout, base_ref):
    """The ``app/`` files that differ from the deployed commit, committed or not, as paths relative to the checkout."""
    def lines(*git):
        out = subprocess.run(["git", "-C", str(checkout), *git], capture_output=True, text=True, check=True).stdout
        return [x for x in out.splitlines() if x.startswith("app/") and not x.endswith(".pyc") and (checkout / x).is_file()]
    return sorted(set(lines("diff", "--name-only", base_ref, "--", "app") + lines("ls-files", "-m", "-o", "--exclude-standard", "app")))


def ship_overlay(meter_say=print):
    """Ship the changed backend files and the whole quest-ai-runner package into the container's /tmp/qoverlay. Returns the file list."""
    checkout = backend_checkout()
    files = changed_backend_files(checkout, need("QAR_OVERLAY_BASE_REF"))
    host, directory = ssh_target()
    stage = Path(os.environ.get("TMPDIR", "/tmp")) / f"qoverlay-stage-{os.getpid()}"
    subprocess.run(["rm", "-rf", str(stage)], check=True)
    (stage / "qar").mkdir(parents=True)
    pack = subprocess.run(["tar", "-C", str(checkout), "-cf", "-", *files], capture_output=True, check=True).stdout
    subprocess.run(["tar", "-C", str(stage), "-xf", "-"], input=pack, check=True)
    pack = subprocess.run(["tar", "-C", str(QAR_ROOT), "--exclude=__pycache__", "-cf", "-", "quest_ai_runner"], capture_output=True, check=True).stdout
    subprocess.run(["tar", "-C", str(stage / "qar"), "-xf", "-"], input=pack, check=True)
    archive = subprocess.run(["tar", "-C", str(stage), "-cf", "-", "."], capture_output=True, check=True).stdout
    subprocess.run(["rm", "-rf", str(stage)], check=True)
    done = remote_exec('sh -c "rm -rf /tmp/qoverlay && mkdir -p /tmp/qoverlay && tar -C /tmp/qoverlay -xf -"', data=archive)
    if done.returncode:
        raise RuntimeError(f"overlay could not be shipped: {done.stderr.decode()[-300:]}")
    meter_say(f"chat_quality: overlay shipped: {len(files)} backend file(s) {files} and the quest_ai_runner package")
    return files


def replay_script():
    return backend_checkout() / "scripts" / "checks" / "quest_chat_replay.py"


def replay(payload, user, team):
    """Run the replay for ``payload`` (a list of cases) on the server. Returns (case records, summary, stderr tail)."""
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    with open(replay_script(), "rb") as script:
        done = remote_exec(f"python - --user {user} --team {team} --overlay /tmp/qoverlay --concurrency 1 --cases-b64 {encoded}", stdin=script)
    records, summary = [], {}
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("@@CASE@@ "):
            records.append(json.loads(line[9:]))
        elif line.startswith("@@SUMMARY@@ "):
            summary = json.loads(line[12:])
    return records, summary, done.stderr.decode("utf-8", "replace")[-600:]


def category_of(collection):
    name = str(collection).lower()
    if any(w in name for w in IGNORED_WORDS):
        return None
    for word, category in CATEGORY_WORDS:
        if word in name:
            return category
    return None


def changes_from(writes):
    """``(changes, other)``: the fingerprint-shaped changes the blocked writes of the turn stand for, and the other attempted writes (not quest records) listed apart."""
    changes, other = [], []
    for w in writes:
        if w.get("kind") == "housekeeping" or w.get("store") != "mongo":
            continue
        category = category_of(w.get("collection"))
        if category is None:
            other.append(f"{w.get('collection')}.{w.get('method')}")
            continue
        method = w.get("method", "")
        before = w.get("before") or {}
        if method.startswith("insert") or (method == "update_one" and not before and category != "goals" and "upsert" in str(w.get("payload", "")).lower()):
            change = dict(category=category, change="added", id=w.get("collection"), label=w.get("label", ""))
        elif method.startswith("delete") or "delete" in method:
            change = dict(category=category, change="removed", id=w.get("collection"), label=before.get("name") or before.get("title") or w.get("label", ""))
        else:
            change = dict(category=category, change="changed", id=w.get("collection"), label=w.get("label", ""),
                          before_label=before.get("name") or before.get("title") or "", before_kept=str(before.get("description") or "").strip().startswith(KEPT_MARKER))
        changes.append(change)
    return changes, sorted(set(other))


def build(meter):
    if not configured():
        print("chat_quality: SKIPPED (QAR_PRIVATE_POOR_ANSWERS_DATASET is not set)")
        raise SystemExit(0)
    import prod_qar_readonly as base  # noqa: F401
    import judge as J
    import prod_probes as P
    dataset = os.path.join(os.environ["QAR_PRIVATE_POOR_ANSWERS_DATASET"], "poorly_answered_inputs.jsonl")
    allowed, histories = PC.load_allowed_cases(dataset), PC.load_allowed_histories(dataset)
    rules = P.load_rules(os.environ.get("QAR_PROD_CASE_RULES_FILE"))
    J.OTHER_ACCOUNT_DATA.update(built=True, section="")
    user, quest, team = need("QAR_OVERLAY_USER"), need("QAR_PROD_QUEST_ID"), need("QAR_PROD_TEAM_ID")
    ship_overlay()
    environment = "production data and tiers, this checkout's code overlaid, writes blocked"
    print(f"chat_quality: replaying the production Quest AI chat turn as {user} on quest {quest} (overlay, writes blocked), {len(allowed)} approved case ids")

    def pinned_quest(case, lane_quest):
        """The quest the case's turn is pinned to: none for ``pinned: false``, the case's own quest id when it names one, else the lane's quest."""
        pinned = case.get("pinned")
        if pinned is False:
            return None
        return pinned.strip() if isinstance(pinned, str) and pinned.strip() else lane_quest

    def converse(case):
        if meter.max_calls is not None and meter.calls >= meter.max_calls:
            from common import BudgetExceeded
            raise BudgetExceeded(f"model-call budget {meter.max_calls} reached before {case['id']}")
        message = PC.check_case(case, allowed)
        history = PC.check_history(case, histories)          # refuses prior turns that are not the file's
        prior = [{"role": str(t.get("role") or "user").lower(), "text": str(t.get("text") or "")} for t in case.get("history") or [] if isinstance(t, dict)]
        case = P.expand(P.with_rules(case, rules))
        record = {"id": case["id"], "dataset": case.get("dataset"), "area": case.get("area"), "environment": environment}
        effect, zero = P.structured(case.get("effect")), bool(case.get("zero_writes"))
        if not zero and not effect:
            record.update(error="not run: the case has no zero_writes flag or structured effect, so its allowed writes are unknown", score=None, passed=False)
            return record
        started = time.time()
        payload = [{"id": case["id"], "input": message, "pinned": pinned_quest(case, quest), "history": prior,
                    "input_origin": "app_voice_chat" if case.get("channel") == "voice" else "app_chat"}]
        try:
            records, summary, tail = replay(payload, user, team)
            if not records:
                raise RuntimeError(f"the replay printed no case: {tail}")
            run = records[0]
            if run.get("error") and not run.get("frames"):
                raise RuntimeError(run["error"])
            frames = PC.frames_of(run["frames"])
            reply = run.get("reply") or ""
            messages = P.messages_of(frames)
            changes, other = changes_from(run.get("writes") or [])
            unexpected = PC.unexpected_writes(changes, effect, zero, set())
            changes_text = [f"{c['category']} {c['change']} {c['id']} {c.get('label', '')!r}" for c in changes] + unexpected
            reads_seen = run.get("reads") or []
            truth = P.reads_text(reads_seen)
            judged_case = dict(case, evidence_notes=(f"{case.get('evidence_notes', '')} Environment: the production Quest AI chat turn replayed on production data with the checkout's code, every write blocked and recorded. "
                                                     "The 'changes' listed are the writes the turn ATTEMPTED (none was made); an UNEXPECTED WRITE line is an attempt the case does not allow.").strip())
            if zero:
                record["state"] = dict(ok=not changes, why="no write attempted" if not changes else f"{len(changes)} write(s) attempted on a case that writes nothing")
            sequence, item = P.sequence_note(judged_case, messages, None)
            record["message_standard"] = P.message_standard(judged_case)
            judged_case["evidence_notes"] += " " + sequence
            judged_case["rubric"] = list(judged_case.get("rubric") or []) + [item]
            evidence = J.build_evidence([{"message": message, "events": frames}])
            absent = P.unsupported_figures(evidence.get("reply", ""), truth, message)
            judged_case["evidence_notes"] += " Figures in the reply that appear neither in the turn's reads nor in the user's message: " + (", ".join(absent) or "none") + "."
            record["reads_seen"], record["unsupported_figures"] = reads_seen, absent
            pre = J.precheck(judged_case, evidence, {}, {}, changes_text)
            judged = J.judge(judged_case, evidence, changes_text, pre, truth, {}, None, production=True)
            passed = J.case_passed(judged_case, pre, judged) and not unexpected
            if record.get("state"):
                passed = passed and record["state"]["ok"]
            record.update(evidence=evidence, changes=changes_text, unexpected_writes=unexpected, other_writes=other, precheck=pre, judged=judged, score=J.final_score(pre, judged), passed=passed,
                          messages=messages, attempted_writes=run.get("writes"), model_calls=run.get("model_calls", 0), replay_error=run.get("error"), model_calls_log=run.get("model_calls_log") or [],
                          tool_calls=PC.tool_calls_of(run["frames"]), route=PC.route_of(run["frames"]),
                          frame_trace=[(f.get("t"), f.get("event"), str(f.get("text") or f.get("content") or "")[:110]) for f in run["frames"]][:80])
        except Exception as e:  # noqa: BLE001
            record.update(error=f"{type(e).__name__}: {e}", score=None, passed=False)
        record.setdefault("model_calls", 0)
        record.update(seconds=round(time.time() - started, 1), tokens_in=0, tokens_out=0, tokens_estimated=True)
        record["log_extra"] = {k: record.get(k) for k in ("environment", "model_calls", "seconds", "changes", "unexpected_writes", "other_writes", "messages", "reads_seen", "unsupported_figures",
                                                          "route", "tool_calls", "frame_trace", "attempted_writes", "state", "replay_error", "model_calls_log")}
        try:
            meter.record(max(1, record["model_calls"]))
        except Exception as e:  # noqa: BLE001
            record["budget_exceeded"] = str(e)
        return record
    converse.__module__ = "prod_chat_overlay"
    return converse
