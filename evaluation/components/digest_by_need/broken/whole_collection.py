"""BASELINE: what a first draft writes. Every record of every group goes into one prompt, so the prompt grows with the collection, nothing is cited
or cached, and the second read pays again."""
from evaluation.components.digest_by_need.fixtures import StubModel, groups_for


def build(meter):  # noqa: ARG001
    def digest(case):
        model = StubModel(case)
        for _ in range(2):
            text = "\n".join(f"[{u.source_id}] {u.text}" for g in groups_for(case) for u in g.units)
            model.answer([{"role": "user", "content": f"NEED: {case['need']}\nRECORDS:\n{text}"}])
        return {"cited": [], "context_chars": len(text), "max_prompt_chars": max(len(p) for p in model.prompts), "calls": 1, "rerun_calls": 1, "statuses": {}}
    return digest
