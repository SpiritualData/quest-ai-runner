"""CURRENT: the live registry (``esoi/hubs.csv`` and its two siblings) read together with the live package: the declared
implementations (``quest_ai_runner.core.resolve``), the solution records, the static scan of ``quest_ai_runner/core`` and a
fake hub registered through each area's own configuration entry. Zero model calls, zero network."""
from hub_of_hubs import probes, registry
from quest_ai_runner.core import resolve


def build(meter):  # noqa: ARG001
    facts, caps = registry.all_facts(resolve.names)
    by_area = {f["area"]: f for f in facts}

    def measure(kind, args):
        fact = by_area[args["area"]]
        if kind == "fake_hub":
            return probes.run(args["area"])
        holds = {"registered": fact["registered"], "by_config": fact["config_ok"], "fresh": fact["fresh"],
                 "seam": fact["seam"], "selectable": fact["selectable"], "core_clean": not fact["vendor_branches"],
                 "no_reinvented": not fact["reinvented"]}[kind]
        return {"holds": holds, "days": fact["days"], "status": fact["status"], "capabilities": fact["capabilities"],
                "by_config": fact["by_config"], "declared": fact["declared"],
                "vendor_branches": len(fact["vendor_branches"]), "first": (fact["vendor_branches"] or [""])[0], "reinvented": fact["reinvented"], "waived": fact["waived"]}
    return measure
