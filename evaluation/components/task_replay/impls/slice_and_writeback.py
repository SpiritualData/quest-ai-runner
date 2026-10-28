"""CURRENT, live: the selected iterate-by-spec arm (slice of the specification in the brief, the metric read before and
after by the runner's meters, the metric tool offered and its gate on) through the real deep-run path. PAID marks it as
spending model calls; they go through the Claude CLI on the subscription, so no API dollars. Run it by decision, staged,
with --max-calls and under the heavy-job lock."""
PAID = True
from quest_ai_runner.core.goal_iteration import SliceAndWriteback
from task_replay import replay


def build(meter):
    meter.model = "claude-code-subscription"
    return lambda case: replay.replay(case, dict(replay.CURRENT, iteration=SliceAndWriteback.name), meter)
