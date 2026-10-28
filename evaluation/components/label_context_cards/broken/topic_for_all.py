"""BASELINE: the shortcut a first draft would write, and the generator's own fallback when its sources are empty. Every card is titled
Topic with no description and no keywords, and every edited file is put into the first card. Without metadata a card is invisible to the
store, so the retrieval cases must fail it. Zero model calls."""
from label_context_cards import arms


def build(meter):  # noqa: ARG001
    def label(case):
        if case["kind"] == "retrieve":
            out = arms.retrieve(case, {"title": "Topic", "description": "", "keywords": []})
            out["fallback"] = True
            return out
        return {"placed": {f: case["cards"][0]["id"] for f in case["files"]}}
    return label
