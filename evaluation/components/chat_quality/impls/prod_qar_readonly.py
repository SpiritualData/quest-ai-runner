"""PRODUCTION ARM, WRITES BLOCKED (live model, real lane configuration and data): each case is one fresh chat turn through the
orchestrator a lane builds, in this process, with the lane's own configuration, context sources, preamble, personas and Quest account,
while ``QAR_DRY_RUN_WRITES=1`` (``the_world/tools/dry_run_writes.py``) records every state change instead of making it.

Setup (all outside this repository): ``QAR_PROD_LANE_ENV_FILE`` names the lane's environment file (``KEY=VALUE`` lines, applied over the
process environment, as the lane's service unit does) and ``QAR_CONFIG_FILE`` its TOML. ``QAR_PROD_LANE_DIR`` is the folder the lane runs in.
Without them ``build`` prints SKIPPED and exits 0, so ``esoi run`` stays green on a machine that holds no lane.

What a case records beyond the dev arm: the reads the turn made (``reads``), the writes it would have made (``changes``, each a
``would have done`` line), the model calls it spent (counted per allowed model process), whether it replanned or sent a still-working
line, and ``environment`` = ``production (writes blocked)``. A write the ideal handling calls for counts as the right route: the judge
is told that the changes listed are attempts that were blocked, not failures to act.

PAID in subscription calls (``claude -p``), no API dollars. ``--max-calls`` bounds the model calls of the whole run; one case stops at
``CASE_MODEL_CALL_CAP`` model calls (default 15, ``QAR_PROD_CASE_MODEL_CALLS``) or ``CASE_SECONDS_CAP`` seconds (default 300, ``QAR_PROD_CASE_SECONDS``).
A tool the lane lacks is listed in ``lane_gaps`` and judged apart from the model; a blocked write is never a failure by itself."""
PAID = True
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ENVIRONMENT = "production (writes blocked)"
CASE_MODEL_CALL_CAP = int(os.environ.get("QAR_PROD_CASE_MODEL_CALLS") or 15)
CASE_SECONDS_CAP = int(os.environ.get("QAR_PROD_CASE_SECONDS") or 300)
BLOCKED_NOTE = ("Environment: production with every write blocked. A line under the changes headed 'would have done' is a write the "
                "assistant attempted for the request (the runner's own memory writes are left out) and was stopped from making; it counts as the assistant having acted, so judge whether acting was "
                "the right route, and do not fault a reply for the missing result of a blocked write.")


def read_env_file(path):
    """``KEY=VALUE`` lines of a lane environment file (comments and blank lines skipped, one pair of quotes stripped)."""
    pairs = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        pairs[key.strip().removeprefix("export ").strip()] = value
    return pairs


def configured():
    return bool(os.environ.get("QAR_PROD_LANE_ENV_FILE") and os.environ.get("QAR_CONFIG_FILE"))


def frames_of(event):
    return event.to_dict() if hasattr(event, "to_dict") else dict(event)


def build(meter):
    if not configured():
        print("chat_quality: SKIPPED (QAR_PROD_LANE_ENV_FILE and QAR_CONFIG_FILE are not set; there is no lane to run)")
        raise SystemExit(0)
    os.environ.update(read_env_file(os.environ["QAR_PROD_LANE_ENV_FILE"]))
    os.environ["QAR_DRY_RUN_WRITES"] = "1"
    os.environ.setdefault("QAR_CHAT_HISTORY_DIR", tempfile.mkdtemp(prefix="qar-prod-readonly-chats-"))
    if os.environ.get("QAR_PROD_LANE_DIR"):
        os.chdir(os.environ["QAR_PROD_LANE_DIR"])
    from quest_ai_runner.the_world.tools import dry_run_writes as dry
    dry.install()
    import judge as J
    J.OTHER_ACCOUNT_DATA.update(built=True, section="")      # the dev-account listing is for the dev world; production has no seeded data
    from quest_ai_runner.the_body.configuration.cli_config import _config_from_env
    from quest_ai_runner.the_consciousness.terminal.interactive_session import InteractiveSession
    from quest_ai_runner.the_intelligence.mind.orchestrator import OrchestratorResult
    cfg = _config_from_env(os.environ["QAR_CONFIG_FILE"])
    session = InteractiveSession(cfg, rep_name=os.environ.get("QAR_REP_NAME") or "Assistant", rep_specified=False, persona_specified=False)
    print(f"chat_quality: production lane configuration, writes blocked, Quest {os.environ.get('QUEST_BASE_URL', '?')}, "
          f"per-case caps {CASE_MODEL_CALL_CAP} model calls / {CASE_SECONDS_CAP}s, run budget "
          f"{meter.max_calls if meter.max_calls is not None else 'none'}")

    def converse(case):
        if meter.max_calls is not None and meter.calls >= meter.max_calls:
            from common import BudgetExceeded
            raise BudgetExceeded(f"model-call budget {meter.max_calls} reached before {case['id']}")
        record = {"id": case["id"], "dataset": case.get("dataset"), "area": case.get("area"), "environment": ENVIRONMENT}
        message = case.get("message") or (case.get("messages") or [""])[0]
        dry.RECORDER.clear()
        started = time.time()
        frames, final = [], None
        try:
            session._session_history = []
            session._conv_id = f"qar_chat_{uuid.uuid4().hex}"
            session.pinned_quest, session._goal_id = None, None
            preamble, grounding = session.turn_grounding(message)
            stream = session._orch.run_stream(
                message, transcript="", quest_id=session.bound_quest_id(), rep_preamble=preamble, conv_id=session._conv_id,
                context_meta=grounding,
                cancel_check=lambda: dry.RECORDER.model_calls >= CASE_MODEL_CALL_CAP or time.time() - started > CASE_SECONDS_CAP)
            try:
                for item in stream:
                    if isinstance(item, OrchestratorResult):
                        final = item
                    else:
                        frames.append(frames_of(item))
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
            reply = (final.text if final is not None and final.text else "") or next(
                (f.get("text") for f in reversed(frames) if f.get("type") in ("result", "done") and f.get("text")), "") or ""
            frames.append({"type": "done", "content": reply})
            if dry.RECORDER.briefs or (final is not None and final.kind == "deep"):
                frames.append({"type": "delegated"})
            changes = [f"WOULD HAVE (blocked, not executed): {line.removeprefix('would have done: ')}" for line in dry.RECORDER.lines()]
            judged_case = dict(case, evidence_notes=f"{case.get('evidence_notes', '')} {BLOCKED_NOTE}".strip())
            evidence = J.build_evidence([{"message": message, "events": frames}])
            pre = J.precheck(judged_case, evidence, {}, {}, changes)
            judged = J.judge(judged_case, evidence, changes, pre, "", {}, None)
            record.update(evidence=evidence, changes=changes, precheck=pre, judged=judged, score=J.final_score(pre, judged),
                          passed=J.case_passed(judged_case, pre, judged))
        except Exception as e:  # noqa: BLE001
            record.update(error=f"{type(e).__name__}: {e}", score=None, passed=False)
        snap = dry.RECORDER.snapshot()
        types = [f.get("type") for f in frames]
        record.update(
            seconds=round(time.time() - started, 1), model_calls=snap["model_calls"], reads=snap["reads"], writes=[w for w in snap["writes"] if not w.get("internal")],
            bookkeeping=sum(1 for w in snap["writes"] if w.get("internal")),
            tokens_in=getattr(final, "tokens_in", 0) or 0, tokens_out=getattr(final, "tokens_out", 0) or 0, lane_gaps=sorted(set(snap["missing_tools"])),
            replanned="replan" in types, still_working="interim" in types,
            route=("deep" if snap["briefs"] else "") or (final.kind if final is not None else "none"))
        record["log_extra"] = {k: record.get(k) for k in ("environment", "model_calls", "seconds", "reads", "changes", "bookkeeping", "tokens_in", "tokens_out", "lane_gaps", "replanned", "still_working")}
        meter.record(snap["model_calls"])
        return record
    return converse
