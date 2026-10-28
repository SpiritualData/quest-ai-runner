"""CURRENT, live (``describe-an-attachment:vision_description``): ``prepare_attachments`` sends the image to a vision model with the library's
describe prompt and adds the text to the grounding, for a text-only answering model that then answers. Both calls run on the Claude Code
subscription (``claude -p``, haiku, the image sent as a stream-json block): model calls, not API dollars, and the dollar figure a run
reports is a list-price equivalent. The shipped lane's configured describer is a different model (a Gemini flash-lite tier); this arm
measures the call's value on the keyless path. Stay on the ladder of esoi/metric-run-policy.md and always pass ``--max-calls``."""
PAID = True
import claude_judge
from describe_an_attachment import arms
from quest_ai_runner.core.attachments import prepare_attachments


def build(meter):
    answerer = claude_judge.cli_provider(meter, arms.ANSWER_MODEL)
    vision = arms.VisionCli(claude_path="claude", timeout_seconds=180)
    meter.wrap(vision)

    def answer(case):
        return arms.run(case, answerer, vision, prepare_attachments)
    return answer
