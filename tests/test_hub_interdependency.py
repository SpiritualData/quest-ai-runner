"""Seats that need another seat are wired through QAR's child of the hub: swapping the card-store arm reaches a dependent seat,
and a cycle or an unmet need fails with a named error. Test-only arms, typed with the hub's Protocols; offline, no model call."""
import pytest
from esoi.assemble import AssemblyConfig, AssemblyError
from hubofhubs.the_body.internal_context.card_store.interface import CardStore
from hubofhubs.the_intelligence.subconscious.context_retriever.interface import ContextRetrieval
from hubofhubs.the_world.tools.web_search.interface import SearchBackend

import quest_ai_runner.hub as qar_hub


class DictRepository:
    def load_all(self):
        return {}

    def read(self, card_id):
        return None

    def write(self, card_id, card):
        return True

    def delete(self, card_id):
        return True

    def exists(self, card_id):
        return False

DEPENDENT = "context-retriever"
ARM = "test-needs-a-card-store"
CYCLE_STORE = "test-store-needs-a-retriever"
UNMET_STORE = "test-store-needs-a-search"


class NeedsAStore:
    def __init__(self, store: CardStore):
        self.store = store


class StoreNeedsARetriever:
    def __init__(self, retriever: ContextRetrieval, repository=None):
        self.retriever = retriever


class StoreNeedsASearch:
    def __init__(self, search: SearchBackend, repository=None):
        self.search = search


@pytest.fixture(autouse=True)
def arms():
    child = qar_hub.child()
    for solution, name, target in ((DEPENDENT, ARM, NeedsAStore), (qar_hub.CARD_STORE, CYCLE_STORE, StoreNeedsARetriever),
                                   (qar_hub.CARD_STORE, UNMET_STORE, StoreNeedsASearch)):
        child.register(solution, name, target, replace=True)


def build(selections, repository=None):
    return qar_hub.child().assemble(AssemblyConfig(solutions=(DEPENDENT,), selections=selections,
                                                   options={qar_hub.CARD_STORE: {"repository": repository or DictRepository()}}))


def test_the_dependent_seat_receives_qars_card_store_when_its_repository_arm_is_selected():
    repository = DictRepository()
    system = build({DEPENDENT: ARM, qar_hub.CARD_STORE: qar_hub.CARD_REPOSITORY}, repository)
    assert type(system[DEPENDENT].store).__name__ == "CardRepositoryStore" and system[DEPENDENT].store.repository is repository
    assert system[DEPENDENT].store is system[qar_hub.CARD_STORE]


def test_swapping_the_card_store_arm_reaches_the_dependent_seat():
    system = qar_hub.child().assemble(AssemblyConfig(solutions=(DEPENDENT,), selections={DEPENDENT: ARM, qar_hub.CARD_STORE: "local_card_store"}))
    assert type(system[DEPENDENT].store).__name__ == "LocalCardStore" and system[DEPENDENT].store is system[qar_hub.CARD_STORE]
    assert system.explain(qar_hub.CARD_STORE).winner.layer == "consumer selections"


def test_a_cycle_between_seats_is_named():
    with pytest.raises(AssemblyError, match="dependency cycle: .*card-store.*context-retriever.*card-store|dependency cycle: .*context-retriever.*card-store.*context-retriever"):
        build({DEPENDENT: ARM, qar_hub.CARD_STORE: CYCLE_STORE})


def test_a_need_is_met_by_the_needed_seats_selected_arm():
    system = build({DEPENDENT: ARM, qar_hub.CARD_STORE: UNMET_STORE})
    assert type(system[qar_hub.CARD_STORE].search).__name__ == type(system["web-search"]).__name__


def test_providing_the_missing_seat_resolves_the_need():
    class Search:
        def search(self, query, max_results=5):
            return []
    search = Search()
    system = qar_hub.child().assemble(AssemblyConfig(solutions=(qar_hub.CARD_STORE,), selections={qar_hub.CARD_STORE: UNMET_STORE}), {"web-search": search})
    assert system[qar_hub.CARD_STORE].search is search
