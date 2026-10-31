"""LIVE ARM: the production ``oversee()`` call against the real model behind the harness's provider
(``QAR_MODEL_BACKEND``, default claude_cli; model ``QAR_EVAL_MODEL``, default haiku), one small call per
scenario (16 calls). Needs a model backend; it cannot run, and so cannot be recorded, on a host without one
(waivers.csv S10 watch-a-run:unmeasured). Run it with --record where a backend exists."""
PAID = True
import os

import common
from quest_ai_runner.core.overseer import oversee


def build(meter):
    model = os.environ.get("QAR_EVAL_MODEL", "haiku")
    provider = common.build_offline_orchestrator(meter).provider

    def signal(digest):
        sig = oversee(provider, model, digest)
        if sig.degraded:
            raise RuntimeError("overseer degraded to proceed (provider unreachable or unreadable verdict)")
        return {"signal": sig.signal, "reason": sig.reason, "hint": sig.hint}
    return signal
