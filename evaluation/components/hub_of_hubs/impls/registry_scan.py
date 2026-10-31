"""CURRENT: the hub's scorer (``hubofhubs``) handed QAR's seat bindings: the registry read together with the live package,
the declared implementations (``quest_ai_runner.core.resolve``), the solution records, the static scan of ``quest_ai_runner/core``
and a fake hub registered through each area's own configuration entry. Zero model calls, zero network."""
from hubofhubs import check, evaluate_seats
from quest_ai_runner.core import resolve

from hub_of_hubs.binding import binding


def build(meter):  # noqa: ARG001
    facts, seat_facts, _connections, _numbers = evaluate_seats(binding(resolve.names))

    def measure(kind, args):
        return check(seat_facts[args["seat"]] if "seat" in args else facts[args["area"]], kind)
    return measure
