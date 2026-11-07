"""Rebuilds ``cases.json``: labelled placements, ``text -> quest(s) or none``, against ``account.QUESTS``.

Each case is a message a person might send in a Quest AI chat, a daily-plan goal, a reflection note or a task form, with the quest ids it
belongs to (``expect``; an empty list means no quest fits and the right answer is none). The ``source`` is a kind of case, not a stage:
``named`` (the quest's name or a close paraphrase), ``topic`` (implied by subject, sharing no word with the quest's title), ``cue`` (a
vague nudge that only a milestone phrase resolves), ``distractor`` (shares words with the wrong quest), ``none`` (no quest fits), ``multi``
(two quests), ``followup`` (names nothing; continues ``previous``). Invented text, no private source."""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from place_on_a_quest import account

CASES = [
    # named
    ("named", "Update me on the mobile app launch", [], ["q_app"]),
    ("named", "How is the kitchen renovation going?", [], ["q_kitchen"]),
    ("named", "Add a note to my half marathon quest: knee felt tight today", [], ["q_run"]),
    ("named", "What is left on the bookkeeper hire?", [], ["q_hire"]),
    ("named", "Show me the newsletter subscriber goal", [], ["q_news"]),
    ("named", "Where are we on the sleep and memory paper?", [], ["q_paper"]),
    ("named", "Open my Spanish learning quest", [], ["q_spanish"]),
    ("named", "Status of the community garden grant", [], ["q_grant"]),
    # topic: implied, no title words
    ("topic", "I ran 12 km this morning and my legs were heavy", [], ["q_run"]),
    ("topic", "The plumber says the sink has to move two feet", [], ["q_kitchen"]),
    ("topic", "Twelve candidates sent in resumes overnight", [], ["q_hire"]),
    ("topic", "Reviewer two wants a larger sample size", [], ["q_paper"]),
    ("topic", "Practiced the subjunctive with my tutor today", [], ["q_spanish"]),
    ("topic", "Welcome email open rate went up to 52 percent", [], ["q_news"]),
    ("topic", "The funder asked how the money for seeds will be spent", [], ["q_grant"]),
    ("topic", "Which hotel has enough rooms for the whole crew in June?", [], ["q_offsite"]),
    ("topic", "The designer sent the new hero image for the landing page", [], ["q_site"]),
    ("topic", "Testers keep reporting a crash on the login screen", [], ["q_app"]),
    # cue: vague, resolved by a milestone phrase
    ("cue", "lets get the apple store release moving", [], ["q_app"]),
    ("cue", "time to chase the building permit", [], ["q_kitchen"]),
    ("cue", "I should line up the references this week", [], ["q_hire"]),
    ("cue", "nudge the co-authors again", [], ["q_paper"]),
    ("cue", "get the verbs flashcards done tonight", [], ["q_spanish"]),
    ("cue", "work on the referral thing", [], ["q_news"]),
    ("cue", "send out the agenda before Friday", [], ["q_offsite"]),
    ("cue", "finish the budget narrative", [], ["q_grant"]),
    ("cue", "book the funder call", [], ["q_grant"]),
    ("cue", "buy shoes before the long run", [], ["q_run"]),
    # distractor: shares words with the wrong quest
    ("distractor", "Launch the new homepage on Monday", [], ["q_site"]),
    ("distractor", "Publish the Android build on Google Play", [], ["q_app"]),
    ("distractor", "Send the budget to the garden partners", [], ["q_grant"]),
    ("distractor", "Grow the signup form conversion on the blog", [], ["q_news"]),
    ("distractor", "Interview the venue managers for June", [], ["q_offsite"]),
    ("distractor", "Rewrite the discussion of the pricing in the paper", [], ["q_paper"]),
    ("distractor", "Set up payroll for the new bookkeeper", [], ["q_hire"]),
    ("distractor", "Email the remote staff their flight details", [], ["q_offsite"]),
    # none
    ("none", "What is the weather like tomorrow?", [], []),
    ("none", "Remind me to call my mother on Sunday", [], []),
    ("none", "Tell me a joke", [], []),
    ("none", "What does compound interest mean?", [], []),
    ("none", "Help me write a thank you note to a neighbor", [], []),
    ("none", "How do I fix a flat bicycle tire?", [], []),
    ("none", "Summarize this article about volcanoes", [], []),
    ("none", "Plan a birthday dinner for Saturday", [], []),
    # multi
    ("multi", "Update the app launch and the website redesign status", [], ["q_app", "q_site"]),
    ("multi", "Compare the contractor quotes and check the bookkeeper references", [], ["q_kitchen", "q_hire"]),
    ("multi", "Add the grant call and the newsletter referral to my week", [], ["q_grant", "q_news"]),
    # followup: names nothing, continues previous
    ("followup", "and what is next for it?", ["How is the kitchen renovation going?"], ["q_kitchen"]),
    ("followup", "who did we hear back from?", ["What is left on the bookkeeper hire?"], ["q_hire"]),
    ("followup", "remind me when that is due", ["Status of the community garden grant"], ["q_grant"]),
]


def build():
    # The account is the other half of every case: its fingerprint is in each case so a changed account changes the cases hash the results are pinned to.
    fingerprint = hashlib.sha256(json.dumps(account.QUESTS, sort_keys=True).encode()).hexdigest()[:8]
    out = []
    for n, (source, message, previous, expect) in enumerate(CASES, start=1):
        out.append({"id": f"PQ-{n:02d}", "source": source, "message": message,
                    "previous": previous[0] if previous else "", "expect": expect, "account": fingerprint})
    return out


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    (here / "cases.json").write_text(json.dumps(build(), indent=1) + "\n")
    print(len(CASES), "cases")
