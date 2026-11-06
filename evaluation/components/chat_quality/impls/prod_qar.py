"""PRODUCTION ARM, BOUNDED WRITES (live model, real lane configuration and data): the same turn as ``prod_qar_readonly``, but the writes a case's
``effect`` allows are made for real and every other state change stays blocked and recorded (``the_world/tools/dry_run_writes.py``).

A case that asks for an action names it in ``effect`` (``{"allow": [{"tool": "<tool>", "args": {...}}, {"method": "POST", "path": "<regex>"}]}``)
and how to tell it happened in ``state_check`` (``{"read": "<GET path>", "list_key": "...", "match": {"field": "text"}, "expect": 1}``). The arm
counts the matching items before and after the turn, reports both counts, and fails a case that left a duplicate or left the expected one
missing; on a repeat the judge also requires the reply to say it is already there. A case with neither field can change nothing.
See ``prod_qar_readonly.py`` for the setup, the caps and the record. PAID in subscription calls (``claude -p``), no API dollars."""
PAID = True
ALTERNATIVE = True   # runs the live brain on a different Quest account and data than dev_quest_chat; its number is compared per environment
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prod_qar_readonly as base  # noqa: E402


def build(meter):
    return base.build_arm(meter, bounded=True)
