"""CURRENT: the arm over a stubbed model. A body is read window by window for the need, the second read is served by the cache. Zero spend."""
from evaluation.components.digest_by_need.fixtures import StubModel, groups_for
from quest_ai_runner.the_intelligence.subconscious.digest_by_need import DigestByNeed
from quest_ai_runner.the_intelligence.subconscious.digest_sources import MemoryDigestCache


def build(meter):  # noqa: ARG001
    def digest(case):
        model, cache = StubModel(case), MemoryDigestCache()
        arm = DigestByNeed(model, cache=cache)
        first = arm.digest(case["need"], groups_for(case), need_key=case["id"])
        calls = len(model.prompts)
        arm.digest(case["need"], groups_for(case), need_key=case["id"])
        return {"cited": [i for r in first.groups for f in r.findings for i in f["source_ids"]], "context_chars": len(first.text),
                "max_prompt_chars": max((len(p) for p in model.prompts), default=0), "calls": calls, "rerun_calls": len(model.prompts) - calls,
                "statuses": {r.group_id: r.status for r in first.groups}}
    return digest
