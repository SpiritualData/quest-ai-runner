"""FLOOR: no model and no orchestrator (``end_to_end/impls/rules_only``): a request shaped like a goal or a note is written as one,
whatever else it asked. The least a first draft does; it writes what was not asked when the shapes mislead it. Zero model calls."""
from end_to_end.impls import rules_only


def build(meter):
    return rules_only.build(meter)
