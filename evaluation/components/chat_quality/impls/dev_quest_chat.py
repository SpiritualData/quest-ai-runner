"""LIVE ARM (opt-in): the real in-app Quest chat on a disposable dev world, judged by ``claude -p``.

Needs a dev Quest (``QAR_EVAL_DEV_ENV_FILE``) and a built world (``runner.py setup``); without them ``build`` prints SKIPPED
and exits 0, so the offline suite and ``esoi run`` stay green. The chat under test is the Quest AI that runs this
checkout's ``quest_ai_runner`` brain; the version below ties a result to that code. Spends model tokens on dev.

Cases come from the component's whole set (``score.load_cases``); draw a few with ``--sample N`` (or name them with
``--only``). Each case runs with the lifecycle of a serial run (``runner.run_serial_case``): a fresh conversation, the
world put back afterwards, the cards it learned swept. The command holds the world lock for its whole run.

Spend. With ``QUAL_INPROCESS=1`` (quest-backend's interpreter, the app served in this process) the chat model is pinned
by ``QUAL_INPROCESS_MODEL`` and every model call is counted: ``model_calls``, tokens and an estimated list-price
dollar figure are real, and ``--max-calls`` stops the run before the next case once the budget is passed. Over HTTP
the server's own models answer and nothing is counted, so ``model_calls`` is one per case (a case is several calls).
Either way the judge is ``claude -p`` on the subscription and is not in the dollar figure."""
import atexit
import os
import sys

from quest_ai_runner import __version__ as brain_version
from quest_ai_runner import pricing
from quest_ops import dev_world

def list_price(models, prompt_tokens, completion_tokens):
    """Estimated dollars for the tokens, at the served model's own provider, input rate and output rate in ``pricing``."""
    model = models[0] if models else "haiku"
    provider = pricing.provider_for_model(model)
    input_rate = pricing.get_input_cost_per_mtok(provider, model) or 0.0
    output_rate = pricing.get_output_cost_per_mtok(provider, model) or 0.0
    return (prompt_tokens * input_rate + completion_tokens * output_rate) / 1e6


def build(meter):
    if not dev_world.configured():
        print("chat_quality: SKIPPED (QAR_EVAL_DEV_ENV_FILE is not set; there is no offline arm for a dev-Quest chat)")
        raise SystemExit(0)
    dev_world.client()  # loads devclient with the env file, refusing a non-dev backend
    import inprocess
    import runner
    import world
    from devclient import INPROCESS, WorldLock
    lock = WorldLock(f"components/run.py chat_quality {' '.join(sys.argv[2:])}")
    lock.__enter__()
    atexit.register(lock.__exit__, None, None, None)
    state = world.load()
    served = None
    if INPROCESS:
        inprocess.client()  # builds the app now, so the pinned models are printed before any case spends
        served = f"in-process, pinned {os.environ.get('QUAL_INPROCESS_MODEL') or 'from the backend .env'}"
    print(f"chat_quality: driving the dev chat ({served or 'over HTTP, the server picks the models'}), "
          f"quest_ai_runner {brain_version}, {len(state['quests'])} world quests, "
          f"model-call budget {meter.max_calls if meter.max_calls is not None else 'none'}")
    holder = {"world": state}

    def converse(case):
        if meter.max_calls is not None and meter.calls >= meter.max_calls:
            from common import BudgetExceeded
            raise BudgetExceeded(f"model-call budget {meter.max_calls} reached before {case['id']}")
        before = inprocess.usage_totals() if INPROCESS else None
        record, holder["world"] = runner.run_serial_case(case, holder["world"])
        runner.save_result(record)  # the transcript and the judge's reasons, for reading why a case failed
        if INPROCESS:
            after = inprocess.usage_totals()
            meter.record(after[0] - before[0])
            meter.tokens_in += after[1] - before[1]
            meter.tokens_out += after[2] - before[2]
            meter.model = ",".join(inprocess.served_models()) or None
            meter.cost_usd = list_price(inprocess.served_models(), meter.tokens_in, meter.tokens_out)
        else:
            meter.record()
        return record
    return converse
