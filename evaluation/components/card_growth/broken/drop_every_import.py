"""BASELINE: the cleanup a first draft writes, every card whose id carries an import namespace (``--``) taken out.
It removes the compounded copies, and with them the only copy of every topic the store holds by import, so a
lane forgets the nested store's knowledge. Zero model calls."""
from pathlib import Path


def build(meter):  # noqa: ARG001
    def settle(cards_dir):
        for entry in Path(cards_dir).glob("*--*.json"):
            entry.unlink()
    return settle
