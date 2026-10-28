"""SELECTED TODAY, live (`ground-in-intent:none`): the same model, prompt and judge with the intent frame left out, so a quest-scoped
prompt carries the quest alone. The gain of the frame is the difference between this arm and ``frame_line`` on the same cases. A
control (a case with no intent above its quest) composes the same bytes for both arms. Subscription calls like ``frame_line``."""
PAID = True
from intent_grounding import arms
from quest_ai_runner.runner.autopilot_briefs import compose_batch_text


def build(meter):
    # compose_batch_text without its intent_frame argument adds no block: that is the arm
    assert compose_batch_text("x", None) == compose_batch_text("x", None, intent_frame=None)
    return arms.live_arm(meter, with_intent=False)
