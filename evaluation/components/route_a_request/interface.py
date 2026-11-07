"""The route-a-request function. Every solution is ONE function with this signature:

    route(kind: str, args: dict) -> str

``args`` = {"request": the person's message, "context": {"view": what the Mind can already see (may be empty)}}. The solution returns one of
``information`` (the person asks to be told something), ``act`` (asks for something to be done or changed), ``draft`` (asks for text written and handed back in the reply)
or ``clarify`` (the request cannot be acted on yet). ``kind`` is the expected route, passed for reporting only; a solution must not read it.

The cases are invented public requests (``cases.json``) and, when present on the machine, the private labelled set (``private_routes.py``: requests of the private chat-quality
datasets with a route label kept beside the data). An implementation module in ``impls/`` exposes ``build(meter) -> route``; it counts any model call it makes with ``meter``.
"""
