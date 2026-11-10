"""Seeded records and the stub model for the digest_by_need cases. The stub cites the entries a case marks, drops any id it was not shown, and
fails on a group the case names, so a score cannot depend on a real model."""
import json
import re
import threading

from quest_ai_runner.the_intelligence.subconscious.digest_sources import SourceGroup, Unit

PADDING = "Written at the end of a long day with plenty of other words around it to make the record a realistic length for a journal."


def groups_for(case):
    """One ``SourceGroup`` per group of the case: ``count`` entries, those in ``matter`` carrying their own text and the rest filler."""
    out = []
    for spec in case["groups"]:
        matter = {int(n): text for n, text in spec.get("matter", {}).items()}
        units = [Unit(f"{spec['id']}:{n}", f"Entry {n}", f"{matter.get(n) or 'filler entry ' + str(n) + ' about the weather and errands'}. {PADDING}")
                 for n in range(spec["count"])]
        out.append(SourceGroup(spec["id"], spec["title"], units))
    return out


class StubModel:
    """Answers each call from the markers in the prompt: records marked with the case's ``marker`` are cited (plus one invented id the engine must drop)."""

    supports_web_search = False

    def __init__(self, case):
        self.marker, self.fail_on, self.prompts, self.lock = case["marker"], case.get("fail_on", ""), [], threading.Lock()

    def answer(self, messages, *, model=None, system=None, layers=None):
        prompt = messages[0]["content"]
        with self.lock:
            self.prompts.append(prompt)
        if self.fail_on and self.fail_on in prompt:
            raise RuntimeError("The AI could not answer just now.")
        hits = re.findall(rf"\[([^\]]+)\] [^\n]*{self.marker}[^\n]*", prompt.split("RECORDS:", 1)[-1])
        if not hits:
            return json.dumps({"relevant": False, "reason": "These records do not bear on the need.", "findings": []})
        return json.dumps({"relevant": True, "findings": [{"text": f"{len(hits)} record(s) bear on this.", "source_ids": hits + ["invented:0"], "quote": ""}]})

    def plan(self, prompt, *, model, tool_schema, layers=None):
        raise NotImplementedError

    def list_models(self):
        return []

    def web_search(self, *args, **kwargs):
        raise NotImplementedError
