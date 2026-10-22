"""CANDIDATE: the guards the library itself carries, run the way the runner runs them. A store is opened with the
``self_heal`` setting the runner's own builder passes (``world.runner_self_heal``, read from ``config_assembler.py``;
off since commit 6980d7c), and when that setting is on, the tidy that opening triggers moves what is provably a copy
of another card of the store (``adapters/card_store_hygiene.py``). As shipped today the tidy is off, so this arm
measures what a runner does to a store that already inflated: nothing. Zero model calls.

The guards that stop a store growing in the first place (origin ids, a store never importing itself, the write-time
id rule, the one-root rule) act while a store is BUILT, so they are measured by the ``card_store_growth`` arm, which
builds stores through repeated passes; this arm measures the part that acts on a store that already inflated."""
from pathlib import Path

from card_store_growth import world
from quest_ai_runner.adapters.file_context_store import FileContextStore


def build(meter):  # noqa: ARG001
    def settle(cards_dir):
        cards_dir = Path(cards_dir)
        store = FileContextStore(str(cards_dir), repo_root=str(cards_dir.parent), auto_bootstrap=False,
                                 mount_mode="copy", max_cards=0, self_heal=world.runner_self_heal())
        store.tidy()
    return settle
