"""ALTERNATIVE (``describe-an-attachment:skip_images``): no description, a real way to handle an attachment (no vision call, no cost). No vision provider is configured, so ``prepare_attachments`` names the
image and says no describer is configured, and the same text-only model answers from that note. One model call per case (the answer)
where the current arm makes two."""
import claude_judge
from describe_an_attachment import arms


def build(meter):
    answerer = claude_judge.cli_provider(meter, arms.ANSWER_MODEL)

    def answer(case):
        return arms.run(case, answerer, None)
    return answer
