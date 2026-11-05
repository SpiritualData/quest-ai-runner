"""The Quest app's wording for the hub's terminal blocks.

``hubofhubs.terminal`` speaks generic English by default; QAR is the consumer that maps its quests onto the hub's vocabulary, so the
Quest words ("From your Quest", the Quest record kind) are registered here, once, when the terminal is set up. Import this module
before rendering; ``tests/test_terminal_drift.py`` keeps these words equal to the Quest app's English pack.
"""
from hubofhubs.the_consciousness.terminal import strings

QUEST_WORDS = {
    "workTrace.group.records": "From your Quest",
    "workTrace.group.recordsShared": "From your team's Quest",
    "workTrace.recordKind.quest": "Quest",
}

strings.register_overrides(QUEST_WORDS)
