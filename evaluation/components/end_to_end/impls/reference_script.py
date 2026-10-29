"""VALIDITY, offline and scripted: the real orchestrator and tool catalogue with a planner that replays each case's ideal decisions and ideal
reply, and the real ``send`` command with a reference acknowledgement. $0 and deterministic. It shows what the code around the
model does for a model that is exactly right (the tools land on the right quest, the route is read correctly, no guard rewrites a
true reply); it says nothing about how well a real model decides. The live arm is ``qar_live``."""
SIMULATED = True   # meters a stand-in for the model: recorded as model 'simulated' at cost 0.0, compared on calls
from assist_a_terminal_session import arms as terminal_arms
import scripted_brain
from end_to_end import drive


def build(meter):
    def turn(case):
        if case["kind"] == "terminal":
            provider = meter.wrap(terminal_arms.ReplayProvider(lambda messages: case["ideal"]["reply"]))
            return drive.run_terminal(case, lambda: provider)
        world = drive.new_world(case)
        script = case["ideal"]
        provider = meter.wrap(drive.IdealPlanner([dict(d) for d in script["decisions"]], script["reply"]))
        return drive.run_with(case, world, scripted_brain.build_brain(provider, tools=world.registry, web=world.web))
    return turn
