"""LIVE MODEL (the single-case arm of ``act-in-the-world``): the same twelve turns, but the planner is a REAL model instead of a
script of ideal decisions. It is ``end_to_end``'s ``qar_live`` arm (the real orchestrator the way production builds it, the claude CLI
backend on the cheapest tier, the real tool catalogue on the world of ``end_to_end/world.json``), scored here by this component's
rule: the world changed exactly as asked and nothing unrequested was written. PAID in subscription calls: it spends Claude Code
subscription time through ``claude -p``, no API dollars. Stay on the staged ladder of esoi/metric-run-policy.md and always pass
``--max-calls``."""
PAID = True
from end_to_end.impls import qar_live


def build(meter):
    return qar_live.build(meter)
