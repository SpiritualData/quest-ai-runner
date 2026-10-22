"""CANDIDATE: the risk-gated review as committed (``adapters/context_risk_gate.folder_gate``, commit ac61f64).

Cheap structural signals (three or more card stores under one folder, a series of dated or numbered sibling
copies, a very large folder) put a folder in front of the folder review whatever the corpus size; when the review
cannot run, a folder with a stores or series signal is not read. Folders with no signal keep the review's
fail-open behaviour. The library's own function, unchanged."""
from quest_ai_runner.adapters.context_risk_gate import folder_gate


def build(meter):  # noqa: ARG001
    return folder_gate
