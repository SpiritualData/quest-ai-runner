"""ALTERNATIVE: an in-memory ``CardCache`` (keeps the last listing and a queue of failed writes for the life
of the process) plugged into the same live ``QuestApiCardRepository``. Not shipped: it shows what a real
cache buys on this metric and is the shape a durable (disk) cache would take. Zero model calls."""
from cache_cards.driver import run_scenario


class MemoryCardCache:
    def __init__(self):
        self.listing = None
        self.writes = []

    def load_listing(self):
        return self.listing

    def store_listing(self, cards, max_updated_at):
        self.listing = (dict(cards), max_updated_at)

    def enqueue_write(self, card_id, card, team_id):
        self.writes.append((card_id, card, team_id))
        return True

    def pending_writes(self):
        return list(self.writes)


def build(meter):  # noqa: ARG001
    def scenario(kind, args):
        if kind != "scenario":
            raise ValueError(kind)
        return run_scenario(args, MemoryCardCache)
    return scenario
