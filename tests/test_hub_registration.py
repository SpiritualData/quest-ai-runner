"""QAR registers its card repository with the hub at import and assembles it through the hub (offline, no model call)."""
import hubofhubs
from hubofhubs.card import Card

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


def test_the_qar_name_is_registered_at_import():
    assert qar_hub.CARD_REPOSITORY in hubofhubs.names(qar_hub.CARD_STORE)


def test_importing_the_package_twice_is_safe():
    qar_hub.register_with_hub()
    qar_hub.register_with_hub()
    assert hubofhubs.names(qar_hub.CARD_STORE).count(qar_hub.CARD_REPOSITORY) == 1


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
    assert qar_hub.CARD_REPOSITORY in system.reasons[0] and "consumer selections" in system.reasons[0]


def test_the_hub_default_is_untouched_by_qar():
    default = hubofhubs.assemble(hubofhubs.HubConfig(seats=(qar_hub.CARD_STORE,)))
    assert "local_card_store" in default.reasons[0]
