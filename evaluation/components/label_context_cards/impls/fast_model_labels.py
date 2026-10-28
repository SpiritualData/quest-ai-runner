"""CURRENT, live on the Claude Code subscription (``label-context-cards:fast_model_labels``): the shipped ``CardMetadataGenerator`` and
``categorize_files_with_llm`` with `claude -p` on its cheapest tier behind them. No API dollars; model calls are counted.

The updater calls ``model_provider.generate(prompt, model=, max_tokens=, temperature=)``, which none of the repository's providers
defines (they define ``answer`` and ``plan``), so with a real provider the shipped call raises, is caught and logged, and places nothing.
This arm hands the updater a host adapter with a ``generate`` (``live_arm.Pinned``), which measures what the placement prompt does when
it can run; what the shipped wiring does with a real provider is pinned by the offline test, not by this arm. Stay on the ladder and
always pass ``--max-calls``."""
PAID = True
from quest_ai_runner.adapters import card_metadata_generator, context_card_updater
from label_context_cards import arms
import live_arm


def build(meter):
    assert card_metadata_generator.CardMetadataGenerator and context_card_updater.categorize_files_with_llm   # the shipped code is the measured code
    provider, model = live_arm.build(meter, "subscription")
    return lambda case: arms.run_retrieve(case, provider, model) if case["kind"] == "retrieve" else arms.run_place(case, provider, model)
