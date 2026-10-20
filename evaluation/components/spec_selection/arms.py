"""Shared by the arms: the fixture index, a scripted judge, and one runner per case kind over a ``select_specs`` function."""
import json
import threading
from pathlib import Path

INDEX = json.loads((Path(__file__).resolve().parent / "fixture_index.json").read_text())


class ScriptedJudge:
    """Answers each batch with the case's ``relevant`` ids that the batch offered, counting the calls like a real model."""

    def __init__(self, meter, relevant, fail=False):
        self.meter, self.relevant, self.fail, self.lock = meter, set(relevant), fail, threading.Lock()

    def answer(self, messages, *, model, system=None, layers=None):
        with self.lock:
            self.meter.record()
        if self.fail:
            raise RuntimeError("scripted failure")
        text = messages[0]["content"]
        offered = [i for i in self.relevant if f" {i}:" in text or i == "ghost-record"]
        return json.dumps({"ids": offered})


def runner(select, meter):
    def run(kind, args):
        fail = kind == "model_failure"
        provider = None if kind == "lexical" and not args.get("relevant") and "relevant" not in args else ScriptedJudge(meter, args.get("relevant", []), fail)
        vector = args.get("vector")
        if vector == "raise":
            def vector_search(text, k):
                raise RuntimeError("vector store down")
        elif vector is not None:
            def vector_search(text, k):
                return list(vector)[:k]
        else:
            vector_search = None
        kwargs = {key: args[key] for key in ("batch_size", "k_lexical", "expand") if key in args}
        got = select(args["task"], INDEX, provider=provider, model="fast", vector_search=vector_search, **kwargs)
        out = {"ids": got.ids, "calls": got.calls, "has_ghost": "ghost-record" in got.ids}
        if kind == "batches":
            size = args.get("batch_size", 10)
            out["calls_match_batches"] = got.calls == -(-len(got.candidates) // size) > 1
        if kind == "model_failure":
            out["all_batches_failed"] = got.failed_batches == got.calls > 0
        return out
    return run
