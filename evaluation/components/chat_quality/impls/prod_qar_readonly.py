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
``CASE_MODEL_CALL_CAP`` model calls (default 45, ``QAR_PROD_CASE_MODEL_CALLS``; a plain read-and-answer turn on this path measured 16 to 32, of which about half is the context engine) or ``CASE_SECONDS_CAP`` seconds (default 300, ``QAR_PROD_CASE_SECONDS``).
A tool the lane lacks is listed in ``lane_gaps`` and judged apart from the model; a blocked write is never a failure by itself."""
PAID = True
import json
import logging
import os
import sys
from collections import Counter
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ENVIRONMENT = "production (writes blocked)"
ENVIRONMENT_BOUNDED = "production (bounded writes)"
CASE_MODEL_CALL_CAP = int(os.environ.get("QAR_PROD_CASE_MODEL_CALLS") or 45)
CASE_SECONDS_CAP = int(os.environ.get("QAR_PROD_CASE_SECONDS") or 300)
BLOCKED_NOTE = ("Environment: production with every write blocked. A line under the changes headed 'would have done' is a write the "
                "assistant attempted for the request (the runner's own memory writes are left out) and was stopped from making; it counts as the assistant having acted, so judge whether acting was "
                "the right route. A reply that says a blocked action could not be made because Quest refused it in test mode is correct and expected; do not fault a reply for the missing result of a blocked write.")


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
    """How many items match a case's ``state_check`` right now, read from Quest (a GET); the spec is the one ``prod_probes.read_number`` reads with ``count``."""
    import prod_probes
    return int(prod_probes.read_number(dict(check, count=True, read=str(check["read"]).replace("{quest_id}", quest_id or str(check.get("quest_id") or ""))), client) or 0)


def state_readback(check, client, quest_id=""):
    """The goals the state check counts, as they stand now: name, assignee id, solution and metric links, period (``prod_probes.readback``)."""
    import prod_probes
    return prod_probes.readback(dict(check, read=str(check["read"]).replace("{quest_id}", quest_id or str(check.get("quest_id") or ""))), client)


def state_verdict(check, before, after):
    """The state check's verdict from its two counts: exactly the expected number afterwards, never more than one created, and a repeat creates none."""
    expect = int(check.get("expect", check.get("expect_min", 1)))
    if (after < expect) if check.get("expect_min") else (after != expect):
        return False, f"expected {'at least ' if check.get('expect_min') else ''}{expect} after the run, found {after}"
    if (after - before > 1 and not check.get("expect_min")) or (before >= expect and after != before):
        return False, f"the run changed the count from {before} to {after}: a repeat must create nothing"
    return True, f"{before} before, {after} after"


RUN_STOPPED = {"why": ""}
log = logging.getLogger("chat_quality.prod_qar")


def goal_in(node, goal_id):
    """The goal with this id anywhere in a listing's nested structure, as ``{title, description}``."""
    if isinstance(node, dict):
        if str(node.get("id") or node.get("goal_id") or "") == str(goal_id) and (node.get("name") or node.get("title")):
            return {"title": node.get("name") or node.get("title"), "description": node.get("description")}
        node = list(node.values())
    if isinstance(node, list):
        for value in node:
            found = goal_in(value, goal_id)
            if found:
                return found
    return None


def goal_lookup(client, quest, team, pause=time.sleep):
    """``lookup(goal_id) -> {title, description} or None`` for the guard in ``dry_run_writes``, which asks before any change to a goal. Fresh reads through ``client.send_request`` of the
    quest's goals, retried, then the quest's current-goals listing (a different route, so a stale or capped team listing does not hide a goal). A read that fails is logged at warning
    level with its cause, never swallowed: a lookup that silently fails blocks every goal write."""
    routes = [f"/api/teams/{team}/quests/{quest}/goals", f"/api/teams/{team}/quests/{quest}/goals", f"/api/planning/quests/{quest}/goals/current"]

    def lookup(goal_id):
        for attempt, path in enumerate(routes):
            try:
                found = goal_in(client.send_request("GET", path) or {}, goal_id)
            except Exception as error:  # noqa: BLE001 -- the next route is tried, and the failure is on record
                log.warning("goal lookup: reading %s failed (%s: %s)", path, type(error).__name__, error)
                found = None
            if found:
                return found
            pause(0.5 * attempt)
        return None
    return lookup


def stop_after_unexpected_write(record, dry):
    """After a case: an unexpected write (reported by the harness, or blocked by the guard) stops the WHOLE run, so one bug cannot reach several goals. Returns the reason, else ""."""
    found = list(record.get("unexpected_writes") or []) + list(dry.RECORDER.snapshot().get("unexpected") or [])
    if found:
        RUN_STOPPED["why"] = f"{record.get('case_id') or 'a case'} made an unexpected write ({found[0][:200]}); the run is stopped"
    return RUN_STOPPED["why"]


def interim_replies_on(environ):
    """Interim replies (the status-first message) are on in this arm as in the Quest app's chat, unless the environment turns them off."""
    return str(environ.get("QUEST_AI_INCREMENTAL_REPLIES_ENABLED", "")).strip().lower() not in ("0", "false", "no", "off")


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
    import prodchat as PC
    import prodsafety as PS
    safety_client = PC.ProdClient(os.environ.get("QUEST_BASE_URL", ""), os.environ.get("QUEST_API_KEY", ""))     # refuses any base URL but the production API; reads, and takes back what a run created
    try:
        who = state_client.whoami() or {}
    except Exception as e:  # noqa: BLE001
        who = {"error": f"{type(e).__name__}: {e}"}
    acted_as = {k: who.get(k) for k in ("user_id", "id", "email", "username", "name", "display_name", "error") if who.get(k)}   # the account every request of this run is made as
    expected = (os.environ.get("QAR_PROD_EXPECT_ACCOUNT") or "").strip().lower()
    if (bounded and not expected) or (expected and expected not in {str(v).lower() for v in acted_as.values()}):
        raise SystemExit(f"chat_quality: this run acts as {acted_as or 'an unknown account'}, but QAR_PROD_EXPECT_ACCOUNT is "
                         f"{expected or 'not set (required for bounded writes)'}; refusing to run")
    PC.check_account(safety_client.whoami())          # the user id and email the wrapper exports (QAR_PROD_EXPECT_USER_ID, QAR_PROD_EXPECT_ACCOUNT)
    components = Path(__file__).resolve().parents[2]
    harness_paths = [os.environ.get("QAR_EVAL_OUTPUT_DIR"), str(components), str(components.parents[1] / "esoi")]
    dry.set_allowances([], harness_paths)

    current_goal_title = goal_lookup(safety_client, PC.target("QAR_PROD_QUEST_ID"), PC.target("QAR_PROD_TEAM_ID"))
    dry.set_goal_title_lookup(current_goal_title)
    import judge as J
    import prod_probes as P
    rules = P.load_rules(os.environ.get("QAR_PROD_CASE_RULES_FILE"))
    J.OTHER_ACCOUNT_DATA.update(built=True, section="")      # the dev-account listing is for the dev world; production has no seeded data
    from quest_ai_runner.the_body.configuration.cli_config import _config_from_env
    from quest_ai_runner.the_consciousness.terminal.interactive_session import InteractiveSession
    from quest_ai_runner.the_intelligence.mind.orchestrator import OrchestratorResult
    cfg = _config_from_env(os.environ["QAR_CONFIG_FILE"])
    if (rules.get("_arm") or {}).get("measure_every_request"):      # the metric of a change is read before and after by code (critical-thinking: every_request_measured)
        cfg.selections = {**(cfg.selections or {}), "critical-thinking": "every_request_measured"}
    session = InteractiveSession(cfg, rep_name=os.environ.get("QAR_REP_NAME") or "Assistant", rep_specified=False, persona_specified=False)
    if interim_replies_on(os.environ):
        session._orch.cfg.interim_replies = True      # on unless the environment turns it off: the Quest app's chat sets it (backend incremental_replies), so an act turn sends its status first
    print(f"chat_quality: acting as {acted_as} on {os.environ.get('QUEST_BASE_URL')}")
    print(f"chat_quality: production lane configuration, {'only the case-allowed writes made, the rest' if bounded else 'writes'} blocked, Quest {os.environ.get('QUEST_BASE_URL', '?')}, "
          f"per-case caps {CASE_MODEL_CALL_CAP} model calls / {CASE_SECONDS_CAP}s, run budget "
          f"{meter.max_calls if meter.max_calls is not None else 'none'}")

    context_views = []              # the context the run was handed, kept as part of what the turn read
    assemblies = Counter()          # how often the turn asked the context assembler, and from where
    assembler = getattr(session._orch, "context_assembler", None)
    if assembler is not None and hasattr(assembler, "assemble"):
        assemble_real = assembler.assemble

        def counted_assemble(*args, **kwargs):
            assemblies[dry.caller_label(2)] += 1
            assembled = assemble_real(*args, **kwargs)
            view = getattr(assembled, "context_view", "")
            if view:
                context_views.append(view)
            return assembled
        assembler.assemble = counted_assemble
    startup = {"calls": len(dry.RECORDER.snapshot()["model_log"])}      # model processes the build and its background work started before any turn

    def converse(case):
        if RUN_STOPPED["why"]:
            return {"id": case["id"], "dataset": case.get("dataset"), "area": case.get("area"), "environment": environment, "score": None, "passed": False,
                    "error": f"not run: {RUN_STOPPED['why']}", "log_extra": {"environment": environment}}
        if meter.max_calls is not None and meter.calls >= meter.max_calls:
            from common import BudgetExceeded
            raise BudgetExceeded(f"model-call budget {meter.max_calls} reached before {case['id']}")
        case = P.with_rules(case, rules)
        record = {"id": case["id"], "dataset": case.get("dataset"), "area": case.get("area"), "environment": environment}
        message = case.get("message") or (case.get("messages") or [""])[0]
        record["startup_calls"], startup["calls"] = startup["calls"], 0
        dry.RECORDER.clear()
        assemblies.clear()
        context_views.clear()
        case = P.expand(case)                      # {monday}, {week} and {today} in the rules are filled for this run
        effect, checks = P.structured(case.get("effect")), P.checks_of(case.get("state_check"))
        allow = effect.get("allow") if (bounded and effect) else None
        dry.set_allowances(allow or [], harness_paths)
        counts = {}
        probe = P.structured(case.get("metric_probe"))
        metric_before = None
        if probe:
            try:
                metric_before = P.read_metric(probe, state_client)
            except Exception as e:  # noqa: BLE001
                record["metric_error"] = f"before: {type(e).__name__}: {e}"
        if bounded and checks:
            try:
                counts["before"] = [count_state(check, state_client) for check in checks]
            except Exception as e:  # noqa: BLE001
                record.update(error=f"not run: the state check cannot be read as {acted_as}: {type(e).__name__}: {e}", score=None, passed=False,
                              acted_as=acted_as)
                record["log_extra"] = {"environment": environment, "acted_as": acted_as}
                return record
        references = {}
        for ref in case.get("reference") or []:        # values read from the records now, shown to the judge as the truth the reply is checked against
            try:
                references[ref["name"]] = P.read_value(ref, state_client)
            except Exception as e:  # noqa: BLE001
                references[ref["name"]] = f"unreadable ({type(e).__name__})"
        zero_case = bool(case.get("zero_writes")) or not bounded           # a writes-blocked arm may create nothing real, whatever the case's effect says
        safety = PS.TurnSafety(safety_client, PC.target("QAR_PROD_EXPECT_USER_ID"), case["id"], effect if bounded else None, zero_case, message)
        try:
            window_reason = safety.gate()
            if window_reason:
                record.update(error=f"not run: {window_reason}", score=None, passed=False, acted_as=acted_as)
                record["log_extra"] = {"environment": environment, "acted_as": acted_as}
                return record
            safety.begin()
        except Exception as e:  # noqa: BLE001 -- never run a turn whose quest state before it cannot be read
            why = "rate limited (the API answered 429 after waiting and one retry; not a result of the product)" if isinstance(e, PC.RateLimited) else f"the quest state before the turn cannot be read: {type(e).__name__}: {e}"
            record.update(error=f"not run: {why}", score=None, passed=False, acted_as=acted_as)
            record["log_extra"] = {"environment": environment, "acted_as": acted_as}
            return record
        started = time.time()
        frames, final = [], None
        try:
            session._session_history = []
            session._conv_id = f"qar_chat_{uuid.uuid4().hex}"
            session.pinned_quest, session._goal_id = None, None
            preamble, grounding = session.turn_grounding(message)
            if case.get("tool_pins"):                    # operations that are offered only when a run names them (for example a spec edit)
                grounding = {**(grounding or {}), "tool_pins": list(case["tool_pins"])}
            # The team cues these inside the quest's own chat, so the turn is pinned to that quest the way the Quest app pins a quest chat
            # (``quest_id`` plus ``quest_ids`` in the turn's scope); the planner never has to ask which quest, nor reads another quest's records.
            pinned = PC.target("QAR_PROD_QUEST_ID")
            if pinned:
                grounding = {**(grounding or {}), "quest_id": pinned, "quest_ids": [pinned]}
            stream = session._orch.run_stream(
                message, transcript="", quest_id=pinned or session.bound_quest_id(), rep_preamble=preamble, conv_id=session._conv_id,
                context_meta=grounding,
                cancel_check=lambda: dry.RECORDER.model_calls >= CASE_MODEL_CALL_CAP or time.time() - started > CASE_SECONDS_CAP)
            try:
                for item in stream:
                    if isinstance(item, OrchestratorResult):
                        final = item
                    else:
                        frames.append(dict(frames_of(item), t=P.stamp(started)))
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
            if final is not None and final.kind == "cancelled":
                why = (f"stopped at the model-call cap ({CASE_MODEL_CALL_CAP})" if dry.RECORDER.model_calls >= CASE_MODEL_CALL_CAP
                       else f"stopped at the time cap ({CASE_SECONDS_CAP}s)")
                raise RuntimeError(f"not judged: the turn was {why} before it replied")
            reply = (final.text if final is not None and final.text else "") or next(
                (f.get("text") for f in reversed(frames) if f.get("type") in ("result", "done") and f.get("text")), "") or ""
            end_seconds = P.stamp(started)         # the reply is in: the last assistant message is stamped here
            frames.append({"type": "done", "content": reply, "t": end_seconds})
            if dry.RECORDER.briefs or (final is not None and final.kind == "deep"):
                frames.append({"type": "delegated"})
            run_calls = dry.RECORDER.model_calls
            changes = [f"WOULD HAVE (blocked, not executed): {line.removeprefix('would have done: ')}" for line in dry.RECORDER.lines()]
            changes += [f"DONE (allowed by the case): {line}" for line in dry.RECORDER.snapshot()["performed"]]
            # What really changed, read from Quest: a DONE line is a call the case allowed to run, not proof that it created anything (a tool may refuse a duplicate).
            # The cleanup runs now, so the state check and the judge see the quest as it stands AFTER the run's own extras were taken back.
            outcome = safety.finish(own_conv=None, frames=frames, executed=dry.RECORDER.snapshot()["performed"])
            changes = [c.replace("DONE (allowed by the case):", "ATTEMPTED (allowed to run, effect not shown by this line):") for c in changes]
            changes += [f"CONFIRMED by reading the quest before and after: {c['category']} {c['change']} {c['id']} {c.get('label', '')!r}" for c in outcome["changes"]]
            changes.append("CONFIRMED by reading the quest before and after: no change" if not outcome["changes"] else outcome["line"])
            note = BLOCKED_NOTE + (" ATTEMPTED lines are calls the case allowed to run; only the CONFIRMED lines show what the quest really holds now, so judge what was created by the "
                                   "CONFIRMED lines and the state counts, never by an ATTEMPTED line. The state counts are read after the cleanup.")
            judged_case = dict(case, evidence_notes=f"{case.get('evidence_notes', '')} {note}".strip())
            if "before" in counts:
                try:
                    counts["after"] = [count_state(check, state_client) for check in checks]
                except Exception as e:  # noqa: BLE001
                    record["state_error"] = f"{type(e).__name__}: {e}"
                try:
                    states = P.after_states(getattr(final, "gathered", None))
                    # The tool's own read-back (the write's response) is the after-state when there is one; the goal list can still be a cached copy, so it is read only without one.
                    record["readback"] = states if states else [state_readback(check, state_client) for check in checks]
                    source = "the tool's read-back of the goal after its write (the saved goal as Quest returned it)" if states else "the quest's goal list read after the run"
                    judged_case["evidence_notes"] += (f" After-state of the goals the state check counts (name, assignee id, solution link, metric link, period), from {source}: "
                                                      + json.dumps(record["readback"], default=str)[:1500] + ".")
                except Exception as e:  # noqa: BLE001
                    record["readback_error"] = f"{type(e).__name__}: {e}"
                owners = P.accepted_owners(effect)
                if owners:
                    judged_case["evidence_notes"] += " Accepted owners for the goal (any one of them is acceptable): " + ", ".join(owners) + ". The after-state carries the assignee's name when the tool could look it up."
            if "before" in counts and "after" in counts:
                verdicts = [state_verdict(check, b4, af) for check, b4, af in zip(checks, counts["before"], counts["after"])]
                record["state"] = dict(counts, ok=all(v[0] for v in verdicts), why="; ".join(v[1] for v in verdicts))
                for check, b4, af in zip(checks, counts["before"], counts["after"]):
                    expect = int(check.get("expect", check.get("expect_min", 1)))
                    if not check.get("expect_min") and af > max(b4, expect):
                        extra = f"UNEXPECTED WRITE: the state count rose from {b4} to {af} where exactly {expect} is expected: this run created {af - max(b4, expect)} extra goal(s)"
                        record["unexpected_writes"] = list(record.get("unexpected_writes") or []) + [extra]
                        judged_case["evidence_notes"] += " " + extra + "."
                        record["passed"] = False
                judged_case["evidence_notes"] += (f" State check: {counts['before']} matching before the run, {counts['after']} after. Lines marked DONE were really made. "
                                                  "Only the writes the case allows run; anything else stays blocked.")
                if P.pre_existing(case, checks, counts["before"]):
                    judged_case["already_there"] = True      # nothing needs doing: one complete message is the standard (prod_probes.message_standard)
                    judged_case["evidence_notes"] += " Pre-existing: yes (the state read before the run already met the expectation, so the right reply says it is already set and where)."
                    judged_case["rubric"] = list(judged_case.get("rubric") or []) + [
                        "The thing already exists, so the reply says it is already set or already there, and does not claim to have created it again"]
            if case.get("zero_writes"):
                attempted = [w for w in dry.RECORDER.snapshot()["writes"] if not w.get("internal")] + [{"done": d} for d in dry.RECORDER.snapshot()["performed"]]
                record["state"] = dict(ok=not attempted, why=("no write attempted" if not attempted else f"{len(attempted)} write(s) attempted on a case that writes nothing"))
            if references:
                judged_case["evidence_notes"] += " Reference values read from the records now: " + "; ".join(f"{k} = {v}" for k, v in references.items()) + "."
            metric = None
            if probe and metric_before is not None:
                try:
                    metric = P.metric_verdict(probe, metric_before, P.read_metric(probe, state_client), case.get("increment_kind", ""))
                    record["metric"] = metric
                except Exception as e:  # noqa: BLE001
                    record["metric_error"] = f"after: {type(e).__name__}: {e}"
            messages = P.messages_of(frames)
            record["messages"] = messages
            sequence, item = P.sequence_note(judged_case, messages, metric)
            record["message_standard"] = P.message_standard(judged_case)
            judged_case["evidence_notes"] += " " + sequence
            judged_case["rubric"] = list(judged_case.get("rubric") or []) + [item]
            evidence = J.build_evidence([{"message": message, "events": frames}])
            reads_seen = P.reads_of(getattr(final, "gathered", None), list(context_views), reply=evidence.get("reply", ""))
            truth = P.reads_text(reads_seen)
            unsupported = P.unsupported_figures(evidence.get("reply", ""), truth, message)
            judged_case["evidence_notes"] += (" Figures in the reply that appear neither in the turn's reads nor in the user's message: " + (", ".join(unsupported) or "none") + ".")
            record["reads_seen"] = [dict(r, text=r["text"][:600]) for r in reads_seen]
            record["unsupported_figures"] = unsupported
            pre = J.precheck(judged_case, evidence, {}, {}, changes)
            judged = J.judge(judged_case, evidence, changes, pre, truth, {}, None, production=True)
            passed = J.case_passed(judged_case, pre, judged)
            if record.get("state"):
                passed = passed and record["state"]["ok"]
            if metric and metric.get("matches_computed") is False:
                passed = False                 # the recorded value differs from the one worked out from the readable data
            record.update(evidence=evidence, changes=changes, precheck=pre, judged=judged, score=J.final_score(pre, judged), passed=passed)
        except Exception as e:  # noqa: BLE001
            record.update(error=f"{type(e).__name__}: {e}", score=None, passed=False)
        try:
            if "outcome" not in locals():           # the turn failed before the cleanup ran: compare and clean now
                outcome = safety.finish(own_conv=None, frames=frames, executed=dry.RECORDER.snapshot()["performed"])
            safety.apply(record, outcome)
            reason = stop_after_unexpected_write(dict(record, case_id=case["id"]), dry)
            if reason:
                record["passed"] = False             # an unexpected write fails the case whatever the verdict says, and stops the run
                record["unexpected_writes"] = list(record.get("unexpected_writes") or []) or list(dry.RECORDER.snapshot().get("unexpected") or [])
                print(f"  {case['id']}: UNEXPECTED WRITE, THE RUN IS STOPPED: {reason}")
        except Exception as e:  # noqa: BLE001
            record.update(passed=False, cleanup_error=f"{type(e).__name__}: {e}", lane_gaps=[f"cleanup could not run: {e}"])
            print(f"  {case['id']}: CLEANUP COULD NOT RUN: {e}")
        snap = dry.RECORDER.snapshot()
        types = [f.get("type") for f in frames]
        judge_chars = int(((record.get("judged") or {}).get("prompt_chars") or 0)) + len(str((record.get("judged") or {}).get("raw") or ""))
        record.update(P.turn_numbers(
            case, record.get("messages") or [], P.stamp(started), run_calls if "run_calls" in locals() else snap["model_calls"],
            getattr(final, "tokens_in", 0) or 0, getattr(final, "tokens_out", 0) or 0, judge_chars, source="orchestrator provider counters (stream)",
            estimated_from=[str(record.get("reads_seen")), (record.get("evidence") or {}).get("reply", "")]))
        record.update(
            seconds=round(time.time() - started, 1), model_calls=run_calls if "run_calls" in locals() else snap["model_calls"],
            calls_by_purpose=dict(Counter(m["label"] for m in snap["model_log"][:(run_calls if "run_calls" in locals() else len(snap["model_log"]))])),
            call_sequence=[(round(m["t"] - started, 1), m["label"].split(" | ")[0].replace("model.", "").replace("answer_with_reasoning", "awr").replace("plan_with_step", "pws"))
                           for m in snap["model_log"][:(run_calls if "run_calls" in locals() else len(snap["model_log"]))]],
            frame_trace=[(f.get("t"), f.get("type"), str(f.get("text") or f.get("content") or f.get("detail") or f.get("label") or "")[:110]) for f in frames][:80],
            housekeeping_skipped=snap["skipped_housekeeping"], context_assemblies=dict(assemblies),
            judge_calls=snap["model_calls"] - (run_calls if "run_calls" in locals() else snap["model_calls"]), performed=snap["performed"], state=record.get("state"), reads=snap["reads"], writes=[w for w in snap["writes"] if not w.get("internal")],
            bookkeeping=sum(1 for w in snap["writes"] if w.get("internal")),
            lane_gaps=sorted(set(snap["missing_tools"]) | set(record.get("lane_gaps") or [])),
            plan_actions=(record.get("evidence") or {}).get("actions"), acted_as=acted_as,
            replanned="replan" in types, still_working="interim" in types,
            route=("deep" if snap["briefs"] else "") or (final.kind if final is not None else "none"))
        record["log_extra"] = {k: record.get(k) for k in ("environment", "model_calls", "seconds", "reads", "changes", "bookkeeping", "tokens_in", "tokens_out", "lane_gaps", "replanned", "still_working", "performed", "state", "judge_calls", "plan_actions", "acted_as", "metric", "messages", "message_standard", "seconds_total", "seconds_to_first_message", "seconds_to_final_message", "message_count", "tokens_total", "tokens_estimated", "judge_tokens", "cost_usd", "numbers_source", "speed_class", "speed_target_seconds", "token_target", "speed_ok", "tokens_ok", "metric_error", "reads_seen", "unsupported_figures", "calls_by_purpose", "call_sequence", "frame_trace", "housekeeping_skipped", "startup_calls", "context_assemblies", "cleanup", "unexpected_writes", "concurrent_writes", "cleanup_error")}
        try:
            meter.record(record["model_calls"])
        except Exception as e:  # noqa: BLE001 -- the run's budget ran out on this turn: keep its record, the next case stops at the top check
            record["budget_exceeded"] = str(e)
        meter.tokens_in += record["tokens_in"]
        meter.tokens_out += record["tokens_out"]
        return record
    converse.__module__ = "prod_qar" if bounded else "prod_qar_readonly"   # the arm name the output log records
    return converse
