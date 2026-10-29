"""QAR is Quest's connector to the hub of hubs: this is the one place QAR meets ``hubofhubs`` at startup.

``hubofhubs`` (the ``hubofhubs`` package of the ai-hub-of-hubs-structure repository) is a required dependency. QAR imports it
once, when ``quest_ai_runner`` is imported, and registers the parts QAR owns under names QAR owns. It registers a part only
where the hub has a Protocol (a seat) for it: today that is the card store. The model call and the web search backend are
external seats the hub never provides, so QAR passes those in ``provided`` where it assembles; they are not registered.
"""
from __future__ import annotations

import hubofhubs

from .adapters.hub_card_store import CardRepositoryStore

CARD_STORE = "card-store"
CARD_REPOSITORY = "qar-card-repository"


def register_with_hub() -> None:
    """Register QAR's parts with the hub. Safe to call twice (a reload replaces QAR's own name)."""
    hubofhubs.register(CARD_STORE, CARD_REPOSITORY, CardRepositoryStore, replace=True)


def assemble(card_repository, *, provided=None):
    """QAR's hub assembly: the ``card-store`` seat is QAR's card repository, with the hub reading it under QAR's selections."""
    config = hubofhubs.HubConfig(seats=(CARD_STORE,), selections={CARD_STORE: CARD_REPOSITORY},
                                 options={CARD_STORE: {"repository": card_repository}})
    return hubofhubs.assemble(config, provided=provided)
