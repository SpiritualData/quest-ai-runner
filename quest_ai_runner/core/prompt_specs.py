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

Pure definitions and one registry; no model call is made here and nothing here knows an org.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from .model_registry import TIERS

# The cache layers of ``prompt_layers.PromptLayers`` plus ``none`` for a one-shot prompt.
CACHE_LAYERS = ("head", "context", "tail", "none")

# A tier chosen by a configured setting, not fixed by the prompt.
CALLER_TIER = "caller"

# Appended by ``render`` to every prompt that tells a model what to write.
NO_EM_DASH_RULE = (
    "Never use em dashes; use two sentences, a comma, a colon, or parentheses instead."
)


@dataclass(frozen=True)
class PromptSpec:
    """One declared prompt. See the module docstring for each field."""

    id: str
    solution: str
    metric: str
    tier: str
    cache_layer: str
    template: str
    inputs: Tuple[str, ...] = ()
    output_schema: Optional[Mapping[str, Any]] = None
    writes_text: bool = True

    def __post_init__(self) -> None:
        if not self.id or not self.solution or not self.metric:
            raise ValueError(f"prompt spec {self.id!r} needs an id, a solution and a metric")
        if self.tier not in TIERS and self.tier != CALLER_TIER:
            raise ValueError(f"prompt spec {self.id!r}: tier {self.tier!r} is not one of "
                             f"{TIERS + (CALLER_TIER,)}")
        if self.cache_layer not in CACHE_LAYERS:
            raise ValueError(f"prompt spec {self.id!r}: cache_layer {self.cache_layer!r} is not "
                             f"one of {CACHE_LAYERS}")

    def render(self, **values: Any) -> str:
        """The prompt text, filled with ``values`` and carrying the no-em-dash rule.

        A spec with no ``inputs`` is returned verbatim (its text may hold literal braces); a spec
        with inputs is filled with ``str.format``. Values must match ``inputs`` exactly, so a typo
        or a forgotten slot fails here rather than reaching a model.
        """
        given, wanted = set(values), set(self.inputs)
        if given != wanted:
            raise ValueError(f"prompt {self.id!r} takes {sorted(wanted)}; got {sorted(given)}")
        text = self.template.format(**values) if self.inputs else self.template
        return with_no_em_dash_rule(text) if self.writes_text else text


def with_no_em_dash_rule(text: str) -> str:
    """``text`` with the no-em-dash rule appended unless it already states it (idempotent)."""
    if "em dash" in text.lower() or "em-dash" in text.lower():
        return text
    return f"{text.rstrip()}\n\n{NO_EM_DASH_RULE}"


SPECS_BY_ID: Dict[str, PromptSpec] = {}


def register(spec: PromptSpec) -> PromptSpec:
    """Add ``spec`` to the registry and return it; an id is registered once."""
    existing = SPECS_BY_ID.get(spec.id)
    if existing is not None and existing != spec:
        raise ValueError(f"prompt id {spec.id!r} is already registered with a different spec")
    SPECS_BY_ID[spec.id] = spec
    return spec


def get_spec(spec_id: str) -> PromptSpec:
    """The registered spec for ``spec_id``; ``KeyError`` names the id when it is unknown."""
    try:
        return SPECS_BY_ID[spec_id]
    except KeyError:
        raise KeyError(f"no prompt spec registered under {spec_id!r}") from None


def render_prompt(spec_id: str, **values: Any) -> str:
    """Render the registered prompt ``spec_id``. Call sites use this with a literal id, which is
    what ``tests/test_prompt_specs.py`` looks for."""
    return get_spec(spec_id).render(**values)
