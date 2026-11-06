"""The hook esoi calls before it hashes this component's cases, so the digest covers the private dataset wherever it is computed (a run, a check, a promote)."""
from context_selection import private_cases


def publish():
    private_cases.publish_digest()
