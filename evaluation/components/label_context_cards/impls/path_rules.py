"""FLOOR (``label-context-cards:path_rules``): no model call. A card's metadata comes from its own sources (its title is the first source's
name, its keywords are the words of the source lines) and an edited file is placed into the card that already holds a file in its folder,
else the card whose title or summary shares a word stem with its path, else into none. The real store and the real retrieval run on it."""
from label_context_cards import arms


def build(meter):  # noqa: ARG001
    return lambda case: arms.run_retrieve(case, None, "") if case["kind"] == "retrieve" else arms.rule_place(case)
