"""QAR is Quest's connector to the hub of hubs: this is the one place QAR meets ``hubofhubs`` at startup.

``hubofhubs`` (the ``hubofhubs`` package of the ai-hub-of-hubs-structure repository) is a required dependency. QAR is a *child*
of the hub: ``child()`` is an ``esoi`` Assembler for scope ``quest_ai_runner`` whose parent is the hub's own. The layers, highest
first: what the caller passes, QAR's own ``selected`` arms (``esoi/solutions.csv`` in a checkout, ``esoi_selection.json`` in an
installed copy), then the hub's defaults. QAR states only what differs from the hub, and each difference carries the ids of QAR's
own problems, ideals or metrics in ``esoi/overrides.csv`` (rule S35). Today the one difference is the ``card-store`` seat.

The model call and the web search backend are external seats the hub never provides, so QAR passes those in ``provided``.
"""
from __future__ import annotations

import dataclasses
import functools
from pathlib import Path

import hubofhubs
from esoi.assemble import Assembler, Assembly, AssemblyConfig

from .adapters.hub_card_store import CardRepositoryStore

SCOPE = "quest_ai_runner"
CARD_STORE = "card-store"
CARD_REPOSITORY = "qar-card-repository"  # the name of QAR's arm; QAR's records select it, this only names it for tests and logs
PACKAGE_DIR = Path(__file__).resolve().parent


@functools.lru_cache(maxsize=1)
def child() -> Assembler:
    """QAR's Assembler, a child of the hub's. A checkout reads QAR's records; an installed copy reads its packaged snapshot."""
    root = PACKAGE_DIR.parent
    records = root if (root / "esoi.toml").is_file() and (root / "esoi").is_dir() else None
    return Assembler(SCOPE, PACKAGE_DIR, records=records, parent=hubofhubs.assembler())


def register_with_hub() -> None:
    """Make QAR's arms known to its Assembler (importing the adapter declares ``qar-card-repository``). Safe to call twice."""
    child().prepare()


def seats() -> list:
    """The solution ids the hub declares: the seats QAR assembles through the hub (QAR's other records are not hub seats)."""
    return hubofhubs.assembler().visible()


def assemble(card_repository, *, provided=None, config=None) -> Assembly:
    """QAR's whole hub assembly: every hub seat built from QAR's choice, else the hub's default. ``card_repository`` is the option
    of QAR's ``card-store`` arm. ``system.pending`` lists the seats nothing could build alone (QAR provides the model call itself).
    ``config`` adds a caller's selections, which beat QAR's own."""
    base = config or AssemblyConfig()
    options = {CARD_STORE: {"repository": card_repository}, **dict(base.options)}
    base = dataclasses.replace(base, options=options)
    given = dict(provided or {})
    assembler = child()
    pending = {sid: line for sid, line in assembler.pending_needs(base, given).items() if sid in seats()}
    wanted = tuple(sid for sid in seats() if sid not in pending)
    built = assembler.assemble(dataclasses.replace(base, solutions=wanted), given)
    return Assembly(assembler, built.config, {sid: (built.objects[sid], built.reason_by_solution[sid], built.choices.get(sid),
                                                    built.implementations.get(sid)) for sid in built.objects}, pending)


def card_repository_of(card_repository):
    """The repository QAR's ``card-store`` seat holds, obtained through the hub assembly. The same object comes back for QAR's own
    arm; an arm swapped in that keeps cards elsewhere has no repository, so the one passed in stands."""
    store = child().assemble(AssemblyConfig(solutions=(CARD_STORE,), options={CARD_STORE: {"repository": card_repository}}))[CARD_STORE]
    return getattr(store, "repository", card_repository)
