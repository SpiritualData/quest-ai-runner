"""CURRENT: the area arm over scripted members. ``choose`` reads the lane's assembler, ``resolve`` and ``find`` hand the question to
the member that owns it, ``search`` ranks through the mixed search. Zero model calls."""
from quest_ai_runner.the_intelligence.subconscious.area_context_retriever import PerLaneAssembly


class Assembler:
    def __init__(self, text):
        self.text = text

    def assemble(self, task_text, *, meta=None, on_event=None):
        return self.text


class Resolver:
    def __init__(self, text):
        self.text = text

    def resolve(self, locator, *, max_chars=2000):
        return self.text


class Index:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, *, scope=None, top_k=8):
        return list(self.hits)


def build(meter):  # noqa: ARG001
    def choose(case):
        arm = PerLaneAssembly(Resolver(case.get("resolved", "")), Index(case.get("hits", [])))
        return {"context": arm.choose(case["task"], assembler=Assembler(case.get("context", ""))),
                "resolved": arm.resolve(case.get("locator", {})), "found": arm.find(case.get("query", "")),
                "searched": list(arm.search(case["task"], []).ids)}
    return choose
