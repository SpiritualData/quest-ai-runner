"""The store-size term of the context-selection score: a store pays for the share of the card limit it fills, on every case.

The balanced score of ``[balance.context_selection]`` folds accuracy, cost and time into one number. Cost has two parts: the model dollars
an arm spends (none for the free arms) and a store charge, set by value before any arm ran: a store that holds as many cards as the card
limit (``QAR_MAX_CARDS``, 1,000 by default) is charged the whole cost budget per case, a store of a tenth of the limit a tenth of it. A
card store is a scarce, shared thing (every card is embedded, uploaded and read), so an arm that needs more cards to do its work pays for
them in the one number, and an arm that keeps no cards (a text search) pays nothing. The charge is recorded as ``cost_usd`` of the row.
"""
import common
from hubofhubs.recall.card_limit import DEFAULT_MAX_CARDS


def cost_budget():
    return float(common.CONFIG.balance["context_selection"]["cost_per_case_usd"])


def store_charged(select, meter, cards=lambda corpus: len(corpus.cards)):
    """``select`` wrapped so each case adds the store charge to ``meter.cost_usd`` and ``select.cards_per_file`` says how many cards
    the store holds per file of the corpus (the topic cases refuse a store of a card per file). ``cards(corpus)`` is the store's size."""
    def charged(task, corpus):
        found = select(task, corpus)
        size = cards(corpus)    # after the call: the first call builds the store
        charged.cards_per_file = size / max(len(corpus.files), 1)
        meter.cost_usd += cost_budget() * min(size / DEFAULT_MAX_CARDS, 1.0)
        return found
    charged.cards_per_file = None
    charged.meter = meter    # the private cases add the cost of the context they select to the same meter
    return charged
