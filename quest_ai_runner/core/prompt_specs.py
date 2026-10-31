"""Prompt specifications: the ONE standard way a model prompt is declared, rendered and found.

Every prompt the library sends to a model is declared as a ``PromptSpec`` beside the code that
uses it and registered here under a stable id. A spec carries what a prompt used to leave
implicit in its call site:

  id            stable name; the only identity a prompt has (Python constant names collide).
  solution      the ESOI solution whose job this prompt does (``esoi/solutions.csv``).
  metric        the metric that justifies the prompt's cost, time and value (``esoi/metrics.csv``);
                it must be one of that solution's metrics. A prompt without a measured reason to
                exist has no business costing a model call.
  tier          the model tier the call is made at (``model_registry.TIERS``), or ``caller`` when a
                configured setting chooses it per deployment.
  cache_layer   where the text sits in ``prompt_layers.PromptLayers``: ``head`` and ``context``
                (cache-eligible prefix), ``tail`` (volatile), or ``none`` (a one-shot prompt that
                shares no prefix with another call).
  inputs        the names the template is filled with; ``render`` refuses missing or extra values.
  output_schema the tool schema or JSON shape the reply must have, or ``None`` for free text.
  template      the prompt text.
  writes_text   True when the prompt tells a model to write text a person may read; ``render``
                then ALWAYS carries the no-em-dash rule (appended when the text lacks it), so the
                brand rule is built into the flow rather than left to each author.

``tests/test_prompt_specs.py`` enforces the standard: a call to a model must go through a
registered spec id (or sit in the shrinking baseline of sites not yet migrated), and every spec
must name a real solution and one of its metrics.

The mechanics (``PromptSpec``, ``PromptRegistry``, the no-em-dash rule) are the hub's, ``hubofhubs.models.prompt_spec``. What stays
here is QAR's binding: its tier names (``model_registry.TIERS``) and its usage log (``usage.tag_spec``), injected into the hub's
spec class, and the one registry every QAR prompt joins. No model call is made here and nothing here knows an org.
"""
from __future__ import annotations

from typing import Any

from hubofhubs.models import prompt_spec as hub

from ..usage import tag_spec
from .model_registry import TIERS

CACHE_LAYERS = hub.CACHE_LAYERS
CALLER_TIER = hub.CALLER_TIER
NO_EM_DASH_RULE = hub.NO_EM_DASH_RULE
with_no_em_dash_rule = hub.with_no_em_dash_rule

# The hub's spec class bound to QAR's tier names and usage log: each render tags the model call that follows.
PromptSpec = hub.bind_spec_class(TIERS, tag_spec)

REGISTRY = hub.PromptRegistry(PromptSpec)
SPECS_BY_ID = REGISTRY.specs
register = REGISTRY.register
declare = REGISTRY.declare
get_spec = REGISTRY.get


def render_prompt(spec_id: str, **values: Any) -> str:
    """Render the registered prompt ``spec_id``. Call sites use this with a literal id, which is
    what ``tests/test_prompt_specs.py`` looks for."""
    return REGISTRY.render(spec_id, **values)
