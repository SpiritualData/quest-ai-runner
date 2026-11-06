"""The hook esoi calls before it hashes this component's cases, so the digest covers the private poorly-answered set wherever it is computed (a run, a check, a promote)."""
from chat_quality import poor_answers


def publish():
    poor_answers.publish_digest()
