"""FLOOR: the shortcut a first draft would write, a repository that keeps its cards in a dict on the object. It
round-trips within one process and nothing else: a second repository on the same storage sees nothing (restart), a
read of an absent card raises (tolerance), and the change stamp never moves (revision). Zero model calls."""
from persist_cards.backends import FolderHarness, on_backend


class DictRepo:
    def __init__(self):
        self.cards = {}

    def load_all(self):
        return dict(self.cards)

    def read(self, card_id):
        return self.cards[card_id]

    def write(self, card_id, card):
        self.cards[card_id] = card
        return True

    def delete(self, card_id):
        del self.cards[card_id]
        return True

    def exists(self, card_id):
        return card_id in self.cards

    def revision(self):
        return 0


def build(meter):  # noqa: ARG001
    return on_backend(FolderHarness, lambda harness: DictRepo())
