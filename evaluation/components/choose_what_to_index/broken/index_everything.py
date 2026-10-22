"""BASELINE: index everything. Extension filtering and the skip list only, no folder decision at all, so every
snapshot's card store is imported or mounted and every snapshot's files are walked. Zero model calls."""
from pathlib import Path

from quest_ai_runner.adapters.context_risk_gate import FolderGate


def build(meter):  # noqa: ARG001
    def gate(walk_root, skip_dirs, provider, model, cards_dir, *, persist=True):  # noqa: ARG001
        return FolderGate(walk_root=Path(walk_root).resolve())
    return gate
