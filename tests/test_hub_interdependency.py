"""Seats that need another seat are wired through QAR's child of the hub: swapping the card-store arm reaches a dependent seat,
and a cycle or an unmet need fails with a named error. Test-only arms, typed with the hub's Protocols; offline, no model call."""
import pytest
from esoi.assemble import AssemblyConfig, AssemblyError
from hubofhubs.seats import CardStore, ContextAssembling, ModelCall

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

DEPENDENT = "context-assembler"
ARM = "test-needs-a-card-store"
CYCLE_STORE = "test-store-needs-an-assembler"
UNMET_STORE = "test-store-needs-a-model"


class NeedsAStore:
    def __init__(self, store: CardStore):
        self.store = store


class StoreNeedsAnAssembler:
    def __init__(self, assembler: ContextAssembling):
        self.assembler = assembler


class StoreNeedsAModel:
    def __init__(self, model: ModelCall):
        self.model = model


@pytest.fixture(autouse=True)
def arms():
    child = qar_hub.child()
    for solution, name, target in ((DEPENDENT, ARM, NeedsAStore), (qar_hub.CARD_STORE, CYCLE_STORE, StoreNeedsAnAssembler),
                                   (qar_hub.CARD_STORE, UNMET_STORE, StoreNeedsAModel)):
        child.register(solution, name, target, replace=True)


def build(selections, repository=None):
    return qar_hub.child().assemble(AssemblyConfig(solutions=(DEPENDENT,), selections=selections,
                                                   options={qar_hub.CARD_STORE: {"repository": repository or DictRepository()}}))


def test_the_dependent_seat_receives_qars_card_store_by_default():
    repository = DictRepository()
    system = build({DEPENDENT: ARM}, repository)
    assert type(system[DEPENDENT].store).__name__ == "CardRepositoryStore" and system[DEPENDENT].store.repository is repository
    assert system[DEPENDENT].store is system[qar_hub.CARD_STORE]


def test_swapping_the_card_store_arm_reaches_the_dependent_seat():
    system = qar_hub.child().assemble(AssemblyConfig(solutions=(DEPENDENT,), selections={DEPENDENT: ARM, qar_hub.CARD_STORE: "local_card_store"}))
    assert type(system[DEPENDENT].store).__name__ == "LocalCardStore" and system[DEPENDENT].store is system[qar_hub.CARD_STORE]
    assert system.explain(qar_hub.CARD_STORE).winner.layer == "consumer selections"


def test_a_cycle_between_seats_is_named():
    with pytest.raises(AssemblyError, match="dependency cycle: .*card-store.*context-assembler.*card-store|dependency cycle: .*context-assembler.*card-store.*context-assembler"):
        build({DEPENDENT: ARM, qar_hub.CARD_STORE: CYCLE_STORE})


def test_an_unmet_need_names_the_seat_and_the_chain():
    with pytest.raises(AssemblyError, match=r"card-store \(test-store-needs-a-model\) needs model-call: no arm, not provided"):
        build({DEPENDENT: ARM, qar_hub.CARD_STORE: UNMET_STORE})


def test_providing_the_missing_seat_resolves_the_need():
    class Model:
        def complete(self, prompt, tier=""):
            return ""
    model = Model()
    system = qar_hub.child().assemble(AssemblyConfig(solutions=(qar_hub.CARD_STORE,), selections={qar_hub.CARD_STORE: UNMET_STORE}), {"model-call": model})
    assert system[qar_hub.CARD_STORE].model is model
