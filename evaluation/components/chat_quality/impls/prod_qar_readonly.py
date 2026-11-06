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
ENVIRONMENT_BOUNDED = "production (bounded writes)"
CASE_MODEL_CALL_CAP = int(os.environ.get("QAR_PROD_CASE_MODEL_CALLS") or 15)
CASE_SECONDS_CAP = int(os.environ.get("QAR_PROD_CASE_SECONDS") or 300)
BLOCKED_NOTE = ("Environment: production with every write blocked. A line under the changes headed 'would have done' is a write the "
                "assistant attempted for the request (the runner's own memory writes are left out) and was stopped from making; it counts as the assistant having acted, so judge whether acting was "
                "the right route. A reply that says a blocked action is done is expected here (the stand-in answered success) and is not a misreport; do not fault a reply for the missing result of a blocked write.")


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


def count_state(check, client, quest_id=""):
    """How many items match a case's ``state_check`` right now, read from Quest (a GET): ``{"read": "/api/quests/{quest_id}/notes", "list_key": "notes",
    "match": {"text": "weekly"}}`` counts the items of that list whose listed fields contain the given text (case-insensitive); the field ``*`` is the
    whole item, so a rule like "one entry per post" can match on the post's own words. The case declares what to read and how to count."""
    payload = client._request("GET", str(check["read"]).replace("{quest_id}", quest_id or str(check.get("quest_id") or "")))
    items = payload.get(check["list_key"]) if isinstance(payload, dict) and check.get("list_key") else payload
    if isinstance(payload, dict) and not check.get("list_key"):
        items = next((v for v in payload.values() if isinstance(v, list)), [])
    match = check.get("match") or {}
    return sum(1 for item in items or [] if isinstance(item, dict) and all(str(want).lower() in str(item if k == "*" else item.get(k, "")).lower() for k, want in match.items()))


def state_verdict(check, before, after):
    """The state check's verdict from its two counts: exactly the expected number afterwards, never more than one created, and a repeat creates none."""
    expect = int(check.get("expect", 1))
    if after != expect:
        return False, f"expected {expect} after the run, found {after}"
    if after - before > 1 or (before >= expect and after != before):
        return False, f"the run changed the count from {before} to {after}: a repeat must create nothing"
    return True, f"{before} before, {after} after"


def build(meter):
    return build_arm(meter, bounded=False)


def build_arm(meter, bounded):
    if not configured():
        print("chat_quality: SKIPPED (QAR_PROD_LANE_ENV_FILE and QAR_CONFIG_FILE are not set; there is no lane to run)")
        raise SystemExit(0)
    lane = read_env_file(os.environ["QAR_PROD_LANE_ENV_FILE"])
    for key in [k for k in os.environ if k.startswith("QUEST_") and k not in lane]:
        del os.environ[key]                # nothing of the calling shell's Quest identity survives: the lane file is the only identity
    os.environ.update(lane)
    for key in ("QUEST_BASE_URL", "QUEST_API_KEY", "QUEST_TEAM_ID"):
        if os.environ.get(key, "") != lane.get(key, ""):
            raise SystemExit(f"chat_quality: {key} does not match the lane environment file; refusing to run")
    os.environ["QAR_DRY_RUN_WRITES"] = "1"
    os.environ.setdefault("QAR_CHAT_HISTORY_DIR", tempfile.mkdtemp(prefix="qar-prod-readonly-chats-"))
    if os.environ.get("QAR_PROD_LANE_DIR"):
        os.chdir(os.environ["QAR_PROD_LANE_DIR"])
    from quest_ai_runner.the_world.tools import dry_run_writes as dry
    dry.install()
    from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestClient
    state_client = QuestClient(os.environ.get("QUEST_BASE_URL", ""), os.environ.get("QUEST_API_KEY", ""), team_id=(os.environ.get("QUEST_TEAM_ID") or ""))
    environment = ENVIRONMENT_BOUNDED if bounded else ENVIRONMENT
    try:
        who = state_client.whoami() or {}
    except Exception as e:  # noqa: BLE001
        who = {"error": f"{type(e).__name__}: {e}"}
    acted_as = {k: who.get(k) for k in ("user_id", "id", "email", "username", "name", "display_name", "error") if who.get(k)}   # the account every request of this run is made as
    expected = (os.environ.get("QAR_PROD_EXPECT_ACCOUNT") or "").strip().lower()
    if (bounded and not expected) or (expected and expected not in {str(v).lower() for v in acted_as.values()}):
        raise SystemExit(f"chat_quality: this run acts as {acted_as or 'an unknown account'}, but QAR_PROD_EXPECT_ACCOUNT is "
                         f"{expected or 'not set (required for bounded writes)'}; refusing to run")
    components = Path(__file__).resolve().parents[2]
    harness_paths = [os.environ.get("QAR_EVAL_OUTPUT_DIR"), str(components), str(components.parents[1] / "esoi")]
    dry.set_allowances([], harness_paths)
    import judge as J
    J.OTHER_ACCOUNT_DATA.update(built=True, section="")      # the dev-account listing is for the dev world; production has no seeded data
    from quest_ai_runner.the_body.configuration.cli_config import _config_from_env
    from quest_ai_runner.the_consciousness.terminal.interactive_session import InteractiveSession
    from quest_ai_runner.the_intelligence.mind.orchestrator import OrchestratorResult
    cfg = _config_from_env(os.environ["QAR_CONFIG_FILE"])
    session = InteractiveSession(cfg, rep_name=os.environ.get("QAR_REP_NAME") or "Assistant", rep_specified=False, persona_specified=False)
    print(f"chat_quality: acting as {acted_as} on {os.environ.get('QUEST_BASE_URL')}")
    print(f"chat_quality: production lane configuration, {'only the case-allowed writes made, the rest' if bounded else 'writes'} blocked, Quest {os.environ.get('QUEST_BASE_URL', '?')}, "
          f"per-case caps {CASE_MODEL_CALL_CAP} model calls / {CASE_SECONDS_CAP}s, run budget "
          f"{meter.max_calls if meter.max_calls is not None else 'none'}")

    def converse(case):
        if meter.max_calls is not None and meter.calls >= meter.max_calls:
            from common import BudgetExceeded
            raise BudgetExceeded(f"model-call budget {meter.max_calls} reached before {case['id']}")
        record = {"id": case["id"], "dataset": case.get("dataset"), "area": case.get("area"), "environment": environment}
        message = case.get("message") or (case.get("messages") or [""])[0]
        dry.RECORDER.clear()
        effect, check = case.get("effect"), case.get("state_check")
        allow = (effect.get("allow") if isinstance(effect, dict) else None) if bounded else None
        dry.set_allowances(allow or [], harness_paths)
        counts = {}
        if bounded and isinstance(check, dict):
            try:
                counts["before"] = count_state(check, state_client)
            except Exception as e:  # noqa: BLE001
                record.update(error=f"not run: the state check cannot be read as {acted_as}: {type(e).__name__}: {e}", score=None, passed=False,
                              acted_as=acted_as)
                record["log_extra"] = {"environment": environment, "acted_as": acted_as}
                return record
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
            run_calls = dry.RECORDER.model_calls
            changes = [f"WOULD HAVE (blocked, not executed): {line.removeprefix('would have done: ')}" for line in dry.RECORDER.lines()]
            changes += [f"DONE (allowed by the case): {line}" for line in dry.RECORDER.snapshot()["performed"]]
            note = BLOCKED_NOTE
            judged_case = dict(case, evidence_notes=f"{case.get('evidence_notes', '')} {note}".strip())
            if "before" in counts:
                try:
                    counts["after"] = count_state(check, state_client)
                except Exception as e:  # noqa: BLE001
                    record["state_error"] = f"{type(e).__name__}: {e}"
            if "before" in counts and "after" in counts:
                ok, why = state_verdict(check, counts["before"], counts["after"])
                record["state"] = dict(counts, ok=ok, why=why)
                judged_case["evidence_notes"] += (f" State check: {counts['before']} matching before the run, {counts['after']} after. Lines marked DONE were really made. "
                                                  "Only the writes the case allows run; anything else stays blocked.")
                if counts["before"] >= int(check.get("expect", 1)):
                    judged_case["rubric"] = list(judged_case.get("rubric") or []) + [
                        "The thing already exists, so the reply says it is already set or already there, and does not claim to have created it again"]
            evidence = J.build_evidence([{"message": message, "events": frames}])
            pre = J.precheck(judged_case, evidence, {}, {}, changes)
            judged = J.judge(judged_case, evidence, changes, pre, "", {}, None)
            passed = J.case_passed(judged_case, pre, judged)
            if record.get("state"):
                passed = passed and record["state"]["ok"]
            record.update(evidence=evidence, changes=changes, precheck=pre, judged=judged, score=J.final_score(pre, judged), passed=passed)
        except Exception as e:  # noqa: BLE001
            record.update(error=f"{type(e).__name__}: {e}", score=None, passed=False)
        snap = dry.RECORDER.snapshot()
        types = [f.get("type") for f in frames]
        record.update(
            seconds=round(time.time() - started, 1), model_calls=run_calls if "run_calls" in locals() else snap["model_calls"],
            judge_calls=snap["model_calls"] - (run_calls if "run_calls" in locals() else snap["model_calls"]), performed=snap["performed"], state=record.get("state"), reads=snap["reads"], writes=[w for w in snap["writes"] if not w.get("internal")],
            bookkeeping=sum(1 for w in snap["writes"] if w.get("internal")),
            tokens_in=getattr(final, "tokens_in", 0) or 0, tokens_out=getattr(final, "tokens_out", 0) or 0, lane_gaps=sorted(set(snap["missing_tools"])),
            plan_actions=(record.get("evidence") or {}).get("actions"), acted_as=acted_as,
            replanned="replan" in types, still_working="interim" in types,
            route=("deep" if snap["briefs"] else "") or (final.kind if final is not None else "none"))
        record["log_extra"] = {k: record.get(k) for k in ("environment", "model_calls", "seconds", "reads", "changes", "bookkeeping", "tokens_in", "tokens_out", "lane_gaps", "replanned", "still_working", "performed", "state", "judge_calls", "plan_actions", "acted_as")}
        meter.record(record["model_calls"])
        meter.tokens_in += record["tokens_in"]
        meter.tokens_out += record["tokens_out"]
        return record
    converse.__module__ = "prod_qar" if bounded else "prod_qar_readonly"   # the arm name the output log records
    return converse
