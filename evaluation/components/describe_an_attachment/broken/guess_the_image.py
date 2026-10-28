"""BASELINE: the shortcut a first draft would write. It never looks at the image or the question and answers as if it had seen a
tidy chart. Zero model calls; the labelled answers are specific values it cannot hold."""


def build(meter):  # noqa: ARG001
    def answer(case):  # noqa: ARG001
        return {"reply": "The image shows a clear chart with a healthy upward trend.", "grounding": "", "described": False}
    return answer
