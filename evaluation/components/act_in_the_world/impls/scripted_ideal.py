"""CURRENT, live: the real orchestrator and the real tool catalogue (the same path as ``end_to_end/impls/reference_script``) with a
planner that replays each case's ideal decisions. It shows what the code around the model does for a model that is exactly
right: every required change lands once on the right quest, and the catalogue, the guards and the route add no write of their
own. It says nothing about how well a real model decides; the live-model arm of the same cases is ``end_to_end/qar_live``.
Zero model calls (the planner is a script, so nothing is metered)."""
import scripted_brain
from end_to_end import drive


def build(meter):  # noqa: ARG001
    def turn(case):
        world = drive.new_world(case)
        script = case["ideal"]
        provider = drive.IdealPlanner([dict(d) for d in script["decisions"]], script["reply"])
        return drive.run_with(case, world, scripted_brain.build_brain(provider, tools=world.registry, web=world.web))
    return turn
