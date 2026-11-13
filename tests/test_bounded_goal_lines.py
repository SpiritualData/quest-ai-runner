"""A quest read hands a model a bounded set of goals: open ones soonest first, done ones represented, the rest counted and described (never all 536)."""
from quest_ai_runner.the_body.internal_context.quest.quest_retrieval_adapter import QuestRetrievalAdapter


def goals(total=536):
    return [{"id": f"g{i}", "name": f"{'Send' if i % 2 else 'Book'} the offer to cohort {i}", "deadline": f"2026-{1 + i % 11:02d}-{1 + i % 27:02d}", "completed": i % 5 == 0} for i in range(total)]


def test_a_536_goal_quest_lists_a_bounded_slice_and_says_what_it_left_out():
    adapter = QuestRetrievalAdapter.__new__(QuestRetrievalAdapter)
    lines = adapter.bounded_goal_lines(goals())
    text = "\n".join(lines)
    from hubofhubs.api import Note
    assert Note(id="x", title="", text=text).body() and len(text) // 4 < 1500 and len(lines) < 536 // 2
    assert "536 in all" in text and "more goals not listed" in text and "ask for a due window" in text
    dues = [l.split("(due ")[1][:10] for l in lines if "(due " in l and "done:" not in l]
    assert dues == sorted(dues)      # open goals come soonest due first


def test_a_small_quest_lists_every_goal_and_no_left_out_line():
    adapter = QuestRetrievalAdapter.__new__(QuestRetrievalAdapter)
    lines = adapter.bounded_goal_lines(goals(6))
    assert not any("not listed" in l for l in lines) and sum(1 for l in lines if l.startswith("  • ")) == 6
