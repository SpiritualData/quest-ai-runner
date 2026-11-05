"""QAR registers its card repository with the hub at import and assembles it through the hub (offline, no model call)."""
import hubofhubs
from hubofhubs.the_body.internal_context.card_store.card import Card

import quest_ai_runner.hub as qar_hub


class DictRepository:
    """The CardRepository surface the adapter uses, in memory."""

    def __init__(self):
        self.cards = {}

    def load_all(self):
        return dict(self.cards)

    def read(self, card_id):
        return self.cards.get(card_id)

    def write(self, card_id, card):
        self.cards[card_id] = card
        return True

    def delete(self, card_id):
        self.cards.pop(card_id, None)
        return True

    def exists(self, card_id):
        return card_id in self.cards


def test_the_qar_name_is_declared_in_the_qar_scope_and_the_hub_never_sees_it():
    assert qar_hub.CARD_REPOSITORY in qar_hub.child().names(qar_hub.CARD_STORE)
    assert qar_hub.CARD_REPOSITORY not in hubofhubs.names(qar_hub.CARD_STORE)


def test_importing_the_package_twice_is_safe():
    qar_hub.register_with_hub()
    qar_hub.register_with_hub()
    assert qar_hub.child().names(qar_hub.CARD_STORE).count(qar_hub.CARD_REPOSITORY) == 1


def test_assembly_gives_a_card_store_over_the_repository_and_names_the_layer():
    repository = DictRepository()
    system = qar_hub.assemble(repository)
    store = system[qar_hub.CARD_STORE]
    assert store.add(Card("a", "context cards remember", "qar", ("memory",))) == "a"
    assert repository.cards["a"]["summary"] == "context cards remember"
    assert store.get("a") == Card("a", "context cards remember", "qar", ("memory",))
    assert [c.id for c in store.search("cards", tag="memory")] == ["a"]
    assert store.search("absent") == []
    assert store.count() == 1
    assert store.delete("a") and not store.delete("a")
    assert qar_hub.CARD_REPOSITORY in system.reason_by_solution[qar_hub.CARD_STORE]   # a caller's repository selects the arm that holds it


def test_the_hub_default_is_untouched_by_qar():
    default = hubofhubs.assemble(hubofhubs.AssemblyConfig(solutions=(qar_hub.CARD_STORE,)))
    assert "local_card_store" in default.reasons[0]


def test_the_assembly_reports_what_the_hub_could_not_build_alone():
    system = qar_hub.assemble(DictRepository())
    assert qar_hub.CARD_STORE not in system.pending and system.pending   # the seats that need a key or a program stay pending


def test_the_card_store_override_comes_from_qars_records_and_cites_its_own_ids():
    one = qar_hub.child().overrides()
    card = next(o for o in one if o.solution_id == qar_hub.CARD_STORE)
    assert card.parent_default == "local_card_store" and card.chosen == "quest-api-cards" and card.justified
    assert "prob-context-drifts-from-its-source" in card.reasons
    assert all(o.justified for o in one)


def test_the_production_call_site_gets_the_same_repository_object_back():
    repository = DictRepository()
    assert qar_hub.card_repository_of(repository) is repository
