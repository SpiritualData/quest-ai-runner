"""BASELINE (offline validity arm, no Quest and no model): proves the end-to-end metric itself is sound before any
dev-Quest spend. For each case it checks that the case validates against the dataset schema, that the
deterministic pre-checks (``judge.precheck``) accept a reference reply built from the case's own expected phrases
with the routing the case expects, and that they reject an empty reply. A case whose checks cannot tell a good
reply from none would score every chat as a pass or every chat as a fail. It says nothing about how well Quest AI
answers; that is ``dev_quest_chat``."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import judge  # noqa: E402
import runner  # noqa: E402

REPLY_CHECKS = ("reply_contains_all", "reply_contains_any", "reply_not_contains")


def reference_reply(case):
    """The shortest reply that satisfies the case's own reply checks."""
    pre = case.get("precheck") or {}
    parts = list(pre.get("reply_contains_all", []))
    if pre.get("reply_contains_any"):
        parts.append(pre["reply_contains_any"][0])
    return " ".join(parts) or "Done."


def evidence(case, reply):
    deep = case.get("expected_routing") in ("deep", "delegated")
    return {"reply": reply, "errors": [], "kind": "deep" if deep else "inline", "actions": [], "delegated": [],
            "tools": [], "web_read": None, "code_text": ""}


def build(meter):  # noqa: ARG001
    def converse(case):
        runner.validate_case(case, "reference_replies")
        reply_only = dict(case, precheck={k: v for k, v in (case.get("precheck") or {}).items() if k in REPLY_CHECKS},
                          expect_writes=[], steps=[])
        accepted = judge.precheck(reply_only, evidence(case, reference_reply(case)), None, None, [])
        rejected = judge.precheck(reply_only, evidence(case, ""), None, None, [])
        ok = not accepted["hard_failures"] and bool(rejected["hard_failures"])
        note = "reference reply accepted, empty reply rejected" if ok else (
            f"accepted={accepted['hard_failures']} rejected_empty={bool(rejected['hard_failures'])}")
        return {"passed": ok, "score": 1.0 if ok else 0.0, "precheck": accepted, "judged": {"summary": note},
                "error": None, "seconds": 0}
    return converse
