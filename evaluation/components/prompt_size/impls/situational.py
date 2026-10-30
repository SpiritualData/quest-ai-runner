"""ALTERNATIVE: the planner prompt assembled per turn from only the rules that apply (``planner_prompt_profile`` is
"situational"), every other assembly point as the live path builds it. It differs from ``live`` only where a
``planner_situation`` case names the turn's facts: the rules whose condition rules the turn out are left out. Zero model
calls; the full-profile text of the same turn is one ``profile: "full"`` call away, so each case is compared with it."""
from prompt_size.impls import live


def build(meter):
    return live.build(meter, profile="situational")
