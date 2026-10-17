"""LIVE ARM (opt-in): the real in-app Quest chat on a disposable dev world, judged by ``claude -p``.

Needs a dev Quest (``QAR_EVAL_DEV_ENV_FILE``) and a built world (``runner.py setup``); without them ``build`` prints SKIPPED
and exits 0, so the offline suite and ``components/run.py`` stay green. The chat under test is the Quest AI that runs this
checkout's ``quest_ai_runner`` brain; the version below ties a result to that code. Spends model tokens on dev."""
import sys

from quest_ai_runner import __version__ as brain_version
from quest_ops import dev_world


def build(meter):
    if not dev_world.configured():
        print("chat_quality: SKIPPED (QAR_EVAL_DEV_ENV_FILE is not set; there is no offline arm for a dev-Quest chat)")
        raise SystemExit(0)
    dev_world.client()  # loads devclient with the env file, refusing a non-dev backend
    import runner
    import world
    state = world.load()
    print(f"chat_quality: driving the dev chat, quest_ai_runner {brain_version}, {len(state['quests'])} world quests")

    def converse(case):
        meter.record()
        return runner.run_case(case, state)
    return converse
