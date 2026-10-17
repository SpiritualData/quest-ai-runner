"""BASELINE: trust the worker and the reviewer without checking. A reply is parsed by looking for the word
true, a worker's ``met`` is believed even when it also reports an error or a human decision, the goal loop
stops after one run whatever the verifier thinks, and every marker or envelope is taken at face value. The
floor the production contract must beat. Zero model calls; no worker is started."""
import json
import re


def build(meter):  # noqa: ARG001
    def contract(kind, args):
        if kind == "review_parse":
            t = args["text"] or ""
            return {"stop": "true" in t.lower(), "reason": t[:80]}
        if kind == "review_fail_open":
            r = args["reply"]
            if r == "__raise__":
                raise RuntimeError("reviewer down")          # a broken reviewer kills the run's monitor
            return {"stop": "true" in str(r).lower(), "reason": str(r)[:80]}
        if kind == "escalation_id":
            m = re.search(r"QAR-ESCALATED:\s*(\S+)", args["output"] or "")
            return {"id": m.group(1) if m else None}
        if kind == "resume_missing":
            return {"missing": "no conversation found" in (args["stderr"] or "").lower()}
        if kind == "envelope_no_work":
            return {"no_work": bool(json.loads(args["raw"]).get("is_error"))}
        if kind == "worker_output":
            d = json.loads(args["raw"])
            return dict(text=d.get("result", ""), tokens=0, cost=0.0, is_error=bool(d.get("is_error")),
                        subtype=d.get("subtype", ""))
        if kind == "goal_prompt":
            return {"text": f"{args.get('preamble', '')}\n\n{args['brief']}\n\n{args['goal']}"}
        if kind == "goal_contract":
            r = args["result"]
            if r == "__raise__":
                raise RuntimeError("worker crashed")          # the caller sees the crash
            return dict(met=bool(r.get("met")), error=r.get("error"), decision_id=r.get("decision_id"))
        if kind == "goal_kwargs":
            return {"forwarded": ["context_preamble", "resume_session_id", "working_dir"]}  # breaks a plain runner
        if kind == "goal_loop":
            return dict(runner_calls=1, verify_calls=0, final_met=bool((args["runs"][0] or {}).get("met")))
        raise ValueError(kind)
    return contract
