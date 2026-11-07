"""The hook esoi calls before it hashes this component's cases, so the digest covers the private labelled set wherever it is computed (a run, a check, a promote)."""
from route_a_request import private_routes


def publish():
    private_routes.publish_digest()
