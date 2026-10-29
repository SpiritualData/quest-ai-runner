"""The hub-of-hubs function (the metric of ``use-open-source-hubs``). Every solution is ONE function:

    measure(kind: str, args: dict) -> dict

The registry, the eight checks (registered, by_config, fresh, seam, selectable, core_clean, fake_hub, no_reinvented) and the
scorer are defined in the hub repository (``ai-hub-of-hubs-structure``, ``docs/connect-a-hub.md``). This component binds QAR to
them: ``binding.py`` says which QAR module fills which area (``esoi/hub_seats.csv``), ``probes.py`` proves a fake hub can be added
through each area's own configuration entry with zero library edits. Each case expects ``holds`` true. An implementation module in
``impls/`` exposes ``build(meter) -> measure``. No model call, no network.
"""
