"""PRODUCTION ARM, THE REAL QUEST AI CHAT OVER HTTP (live model): each case is one fresh chat conversation pinned to the lane's quest, driven as the AI account
through the same routes the Quest app uses (create a conversation with the quest, stream one message, read the conversation back). The chat's own routing,
scope guards, tools, approvals parking and replies are what is judged; nothing of the lane path runs here.

This is the arm team members' experience is measured by. ``prod_qar`` and ``prod_qar_readonly`` run the lane's in-process orchestrator with stubbed writes and deep runs; this
one cannot stub anything, so safety is by design and verification (``prodchat.py``): only the approved private case ids and their exact texts are sent, only the production
API and the AI account are accepted, the client can make no write but the conversation's own, and the quest is fingerprinted before and after (notes, goals, workflows, spec
records, tasks, decision-requests, the account's chat conversations). A change the case's ``effect`` allow-list does not explain is an UNEXPECTED WRITE and fails the case; a
zero-writes case must show none. Cases that can write are run only on a human decision.

Per case: at most 60 model calls (counted from the stream's token frames) and 6 minutes. Judge: the same chat_quality judge, with the reads built from the stream's read and
tool frames and the stored conversation. PAID in subscription and production model calls; the judge is ``claude -p``."""
PAID = True
ALTERNATIVE = True
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import prodchat as PC  # noqa: E402


def configured():
    return bool(os.environ.get("QAR_PROD_LANE_ENV_FILE"))


def build(meter):
    if not configured():
        print("chat_quality: SKIPPED (QAR_PROD_LANE_ENV_FILE is not set; there is no production key to drive the chat with)")
        raise SystemExit(0)
    import prod_qar_readonly as base
    import judge as J
    import prod_probes as P
    lane = base.read_env_file(os.environ["QAR_PROD_LANE_ENV_FILE"])
    client = PC.ProdClient(lane.get("QUEST_BASE_URL", ""), lane.get("QUEST_API_KEY", ""))     # refuses any base URL but the production API
    acted_as = PC.check_account(client.whoami())                                                 # refuses any account but the AI account
    environment = PC.environment_label()
    allowed = PC.load_allowed_cases(os.path.join(os.environ["QAR_PRIVATE_POOR_ANSWERS_DATASET"], "poorly_answered_inputs.jsonl"))
    rules = P.load_rules(os.environ.get("QAR_PROD_CASE_RULES_FILE"))
    auto_run = os.environ.get("QAR_PROD_CHAT_AUTO_RUN", "1") != "0"
    J.OTHER_ACCOUNT_DATA.update(built=True, section="")
    print(f"chat_quality: driving the production Quest AI chat as {acted_as['email']} on {client.base}, quest {client.quest_id}, "
          f"{len(allowed)} approved case ids, per-case caps {PC.CASE_MODEL_CALL_CAP} model calls / {PC.CASE_SECONDS_CAP}s, run budget "
          f"{meter.max_calls if meter.max_calls is not None else 'none'}")

    def converse(case):
        if meter.max_calls is not None and meter.calls >= meter.max_calls:
            from common import BudgetExceeded
            raise BudgetExceeded(f"model-call budget {meter.max_calls} reached before {case['id']}")
        message = PC.check_case(case, allowed)           # refuses any other id or text before anything is sent
        case = P.expand(P.with_rules(case, rules))
        record = {"id": case["id"], "dataset": case.get("dataset"), "area": case.get("area"), "environment": environment, "acted_as": acted_as}
        effect, checks = P.structured(case.get("effect")), P.checks_of(case.get("state_check"))
        zero = bool(case.get("zero_writes"))
        if not zero and not P.structured(case.get("effect")):
            record.update(error="not run: the case has no zero_writes flag or structured effect, so its allowed writes are unknown", score=None, passed=False)
            record["log_extra"] = {"environment": environment, "acted_as": acted_as}
            return record
        probe = P.structured(case.get("metric_probe"))
        metric_before = None
        try:
            fp_before = PC.read_fingerprint(client)
            counts_before = [base.count_state(c, client, client.quest_id) for c in checks] if checks else None
            if probe:
                metric_before = P.read_metric(probe, client)
        except Exception as e:  # noqa: BLE001 -- never send a message when the state before cannot be read
            record.update(error=f"not run: the quest state before the turn cannot be read: {type(e).__name__}: {e}", score=None, passed=False)
            record["log_extra"] = {"environment": environment, "acted_as": acted_as}
            return record
        references = {}
        for ref in case.get("reference") or []:
            try:
                references[ref["name"]] = P.read_value(ref, client)
            except Exception as e:  # noqa: BLE001
                references[ref["name"]] = f"unreadable ({type(e).__name__})"
        started = time.time()
        started_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started - 5))      # a few seconds early: a clock difference must not hide this run's own item
        print(f"  {case['id']}: {PC.describe_undo(effect, zero)}")
        conv, events = None, []
        cleanup = None
        try:
            conv = client.create_conversation()
            record["conversation"] = conv
            capped = None
            for event in client.stream_turn(conv, message, started, auto_run=auto_run):
                events.append(event)
                if PC.event_name(event) in ("_capped", "_http_error", "_error"):
                    capped = event.get("text")
            if capped:
                raise RuntimeError(f"not judged: {capped}")
            time.sleep(2.0)                             # let the server finish persisting the turn
            fp_after = PC.read_fingerprint(client)
            conversation = client._request("GET", f"/api/quest-ai/conversations/{conv}", params={"include_interim": "true"})
            changes = PC.diff_fingerprints(fp_before, fp_after, own_conv=conv)
            unexpected = PC.unexpected_writes(changes, effect, zero)
            cleanup = PC.run_cleanup(client, PC.plan_cleanup(changes, conv, started_iso, acted_as["user_id"], effect, zero))
            usage = PC.usage_of(events)
            frames = PC.frames_of(events)
            reply = next((f.get("content") for f in reversed(frames) if f["type"] == "done" and f.get("content")), "") or "".join(
                str(f.get("text") or "") for f in frames if f["type"] == "token")[-8000:]
            if not any(f["type"] == "done" for f in frames):
                frames.append({"type": "done", "content": reply, "t": P.stamp(started)})
            messages = P.messages_of(frames)
            stored = PC.stored_messages(conversation)
            judged_case = dict(case, evidence_notes=(f"{case.get('evidence_notes', '')} Environment: the real production Quest AI chat over HTTP, nothing stubbed. "
                                                     "The 'changes' listed are whole-quest differences read before and after the turn; an UNEXPECTED WRITE line is a change the case does not allow.").strip())
            changes_text = [f"{c['category']} {c['change']} {c['id']} {c.get('label', '')!r}" for c in changes]
            changes_text += unexpected
            if checks and counts_before is not None:
                counts_after = [base.count_state(c, client, client.quest_id) for c in checks]
                verdicts = [base.state_verdict(c, b, a) for c, b, a in zip(checks, counts_before, counts_after)]
                record["state"] = dict(before=counts_before, after=counts_after, ok=all(v[0] for v in verdicts), why="; ".join(v[1] for v in verdicts))
            if zero:
                record["state"] = dict(ok=not changes, why="no change to the quest" if not changes else f"{len(changes)} change(s) on a case that writes nothing")
            if references:
                judged_case["evidence_notes"] += " Reference values read from the records now: " + "; ".join(f"{k} = {v}" for k, v in references.items()) + "."
            metric = None
            if probe and metric_before is not None:
                try:
                    metric = P.metric_verdict(probe, metric_before, P.read_metric(probe, client), case.get("increment_kind", ""))
                    record["metric"] = metric
                except Exception as e:  # noqa: BLE001
                    record["metric_error"] = f"after: {type(e).__name__}: {e}"
            sequence, item = P.sequence_note(judged_case, messages, metric)
            record["message_standard"] = P.message_standard(judged_case)
            judged_case["evidence_notes"] += " " + sequence
            judged_case["rubric"] = list(judged_case.get("rubric") or []) + [item]
            evidence = J.build_evidence([{"message": message, "events": frames}])
            reads_seen = PC.reads_from_stream(events)
            truth = P.reads_text(reads_seen)
            unsupported = P.unsupported_figures(evidence.get("reply", ""), truth, message)
            judged_case["evidence_notes"] += " Figures in the reply that appear neither in the turn's reads nor in the user's message: " + (", ".join(unsupported) or "none") + "."
            record["reads_seen"], record["unsupported_figures"] = reads_seen, unsupported
            pre = J.precheck(judged_case, evidence, {}, {}, changes_text)
            judged = J.judge(judged_case, evidence, changes_text, pre, truth, {}, None, production=True)
            passed = J.case_passed(judged_case, pre, judged) and not unexpected and not cleanup["left"]
            if record.get("state"):
                passed = passed and record["state"]["ok"]
            if metric and metric.get("matches_computed") is False:
                passed = False
            record.update(evidence=evidence, changes=changes_text, unexpected_writes=unexpected, precheck=pre, judged=judged, score=J.final_score(pre, judged), passed=passed,
                          messages=messages, stored_messages=stored, fingerprint_changes=changes)
        except Exception as e:  # noqa: BLE001
            record.update(error=f"{type(e).__name__}: {e}", score=None, passed=False)
            try:                                         # the quest is still compared and cleaned, so a write made by a turn that failed is reported and taken back
                failed_changes = PC.diff_fingerprints(fp_before, PC.read_fingerprint(client), own_conv=conv)
                record["unexpected_writes"] = PC.unexpected_writes(failed_changes, effect, zero)
                if cleanup is None:
                    cleanup = PC.run_cleanup(client, PC.plan_cleanup(failed_changes, conv, started_iso, acted_as["user_id"], effect, zero))
            except Exception as inner:  # noqa: BLE001
                record["cleanup_error"] = f"{type(inner).__name__}: {inner}"
        if cleanup is not None:
            record["cleanup"] = PC.cleanup_line(cleanup)
            record["lane_gaps"] = [record["cleanup"]] if (cleanup["withdrawn"] or cleanup["deleted"] or cleanup["left"]) else []
            if cleanup["left"]:
                record["passed"] = False
                print(f"  {case['id']}: CLEANUP FAILED, a human must look: {record['cleanup']}")
        elif record.get("cleanup_error"):
            record["passed"] = False
            record["lane_gaps"] = [f"cleanup could not run: {record['cleanup_error']}"]
            print(f"  {case['id']}: CLEANUP COULD NOT RUN: {record['cleanup_error']}")
        usage = PC.usage_of(events)
        names = [PC.event_name(e) for e in events]
        record.update(seconds=round(time.time() - started, 1), model_calls=usage["model_calls"], tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
                      route=PC.route_of(events), tool_calls=PC.tool_calls_of(events), replanned="replan" in names, still_working="interim" in names,
                      frame_trace=[(e.get("t"), PC.event_name(e), str(e.get("text") or e.get("content") or (e.get("data") or {}).get("text") or "")[:110]) for e in events][:80])
        record["log_extra"] = {k: record.get(k) for k in ("environment", "model_calls", "seconds", "changes", "unexpected_writes", "tokens_in", "tokens_out", "state", "acted_as", "metric",
                                                          "messages", "stored_messages", "message_standard", "metric_error", "reads_seen", "unsupported_figures", "route", "tool_calls",
                                                          "replanned", "still_working", "frame_trace", "conversation", "fingerprint_changes", "cleanup", "lane_gaps", "cleanup_error")}
        try:
            meter.record(max(1, record["model_calls"]))
        except Exception as e:  # noqa: BLE001
            record["budget_exceeded"] = str(e)
        meter.tokens_in += record["tokens_in"]
        meter.tokens_out += record["tokens_out"]
        return record
    converse.__module__ = "prod_quest_chat"
    return converse
