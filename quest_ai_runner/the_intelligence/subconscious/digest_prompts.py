"""The prompts of ``digest_by_need``, declared once and rendered by ``read_window`` into ``PromptLayers``.

A sub-analysis reads ONE group of records for ONE need. The head (the rules, the need and the background it is asked in) is identical for every
group of a call, so a provider's prefix cache serves it; the group's records are the volatile tail. Both texts carry the no-em-dash rule.
"""
from __future__ import annotations

from typing import Sequence

from quest_ai_runner.the_self.integrity.prompts.prompt_specs import declare

HEAD_TEMPLATE = """You are a careful reader. You are given one NEED (a question another step is answering for one person) and then one GROUP of that person's own records, each under a source id in square brackets. Report only what in this group bears on the NEED.

Return ONLY a valid JSON object:
{{
  "relevant": true or false,
  "reason": "<when not relevant: one short sentence on why nothing here bears on the need>",
  "findings": [{{
    "text": "<one finding that bears on the need, in your own words, at most {finding_chars} characters>",
    "source_ids": ["<the source ids in square brackets that this finding rests on, exactly as written>"],
    "quote": "<a short verbatim quote from the record, only when the exact words matter, else empty>"
  }}]
}}

Rules:
- Use only source ids shown in this group, exactly as written. Never invent one. Every finding cites at least one.
- At most {max_findings} findings. Merge records that say the same thing into one finding that cites them all.
- Report what the person wrote or did. Add no advice, no inference about their character, and nothing that is not in the records.
- When nothing bears on the NEED, return relevant false, a reason and an empty findings list.
- {no_em_dash}
- Respond with ONLY the JSON object, no markdown fences and no prose.

NEED (what the next step is answering):
{need}

BACKGROUND:
{background}
"""

TAIL_TEMPLATE = """GROUP: {group_title}
RECORDS:
{records}
"""

HEAD_SPEC = declare("digest-records-for-a-need.digest-head", solution="digest-records-for-a-need", metric="metric-digest-by-need", tier="fast",
                    cache_layer="head", template=HEAD_TEMPLATE)
TAIL_SPEC = declare("digest-records-for-a-need.digest-group", solution="digest-records-for-a-need", metric="metric-digest-by-need", tier="fast",
                    cache_layer="tail", template=TAIL_TEMPLATE, writes_text=False)


def records_block(records: Sequence[Sequence[str]]) -> str:
    """``records`` as ``(source_id, text)`` pairs, each under its id in square brackets."""
    return "\n\n".join(f"[{source_id}] {text}" for source_id, text in records)
