"""CANDIDATE: the guards the library itself carries, run the way the runner runs them. A store is opened with
``self_heal=True`` (the runner's builder does), and the tidy that opening triggers moves what is provably a copy
of another card of the store: a compounded id whose files another card pins, and cards pinning exactly the same
files, keeping the most used. Cards are moved under ``_quarantine/<reason>/``, never deleted, and a card another
system manages or a person taught is never taken (``adapters/card_store_hygiene.py``). Zero model calls.

The guards that stop a store growing in the first place (origin ids, a store never importing itself, the write-time
id rule, the one-root rule) act while a store is BUILT, so they are measured by the ``card_store_growth`` arm, which
builds stores through repeated passes; this arm measures the part that acts on a store that already inflated."""
from pathlib import Path

from quest_ai_runner.adapters.file_context_store import FileContextStore


def build(meter):  # noqa: ARG001
    def settle(cards_dir):
        cards_dir = Path(cards_dir)
        store = FileContextStore(str(cards_dir), repo_root=str(cards_dir.parent), auto_bootstrap=False,
                                 mount_mode="copy", max_cards=0, self_heal=True)
        store.tidy()
    return settle
