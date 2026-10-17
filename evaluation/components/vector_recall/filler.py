"""Deterministic filler cards, so a latency measurement runs at a FIXED corpus size.

``padded(corpus, size)`` returns the corpus with invented harbour notices appended until it holds ``size``
documents. The text is generated from small word lists with a seeded generator (the same call always returns the
same cards), has the handbook's document shape, and holds no real content. The handbook documents keep their
ids; filler ids start with ``notice-``."""
import random

SUBJECTS = ["pontoon", "slipway", "ice store", "net loft", "chandlery", "signal mast", "boat shed", "pilot station",
            "salt pans", "ferry ramp", "lamp room", "tide gauge", "rope walk", "sail loft", "ballast yard"]
TOPICS = ["opening hours", "booking rules", "fee schedule", "safety checks", "winter closure", "repair queue",
          "cleaning rota", "key collection", "lost property", "visitor access", "noise limits", "waste sorting"]
ACTIONS = ["Members book by leaving a note with the duty officer", "Visitors sign the log before entering",
           "The committee reviews the rota each quarter", "A deposit is held until the keys come back",
           "Work stops when the wind passes the posted limit", "Notices go on the board by the main gate",
           "Contractors report to the office before starting", "The caretaker records every inspection"]


def padded(corpus, size):
    docs = list(corpus["docs"])
    rng = random.Random(20261016)
    i = 0
    while len(docs) < size:
        subject, topic = rng.choice(SUBJECTS), rng.choice(TOPICS)
        sections = [{"heading": rng.choice(TOPICS).title(), "text": f"{rng.choice(ACTIONS)}. {rng.choice(ACTIONS)}. "
                     f"Applies to the {rng.choice(SUBJECTS)} on days {rng.randint(1, 28)} to {rng.randint(1, 28)}."}
                    for _ in range(3)]
        docs.append({"id": f"notice-{i:05d}", "title": f"{subject.title()} {topic}",
                     "summary": f"Standing notice about the {subject}: {topic}.", "sections": sections})
        i += 1
    return {**corpus, "docs": docs}
