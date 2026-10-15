"""The prompt-size function. Every solution is ONE function with this signature:

    assemble(point: str, inputs: dict) -> str

``point`` names an assembly point (``planner``, ``autopilot_batch``, ``context_update_bundle``,
``deep_prompt``) and ``inputs`` is the fixed input set ``fixtures.build_inputs`` made for the case.
The function returns the prompt text exactly as it would be handed to a model. The scorer measures
that text (total characters, duplicated blocks); the solution decides how the pieces are budgeted,
deduplicated and cut.

An implementation module in ``impls/`` exposes ``build(meter) -> assemble``. No solution here calls a model.
"""
