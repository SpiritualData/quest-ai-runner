"""CURRENT, live on the fast tier the shipped configuration pins (``label-context-cards:fast_model_labels``, priced): the same generator
and placement as ``fast_model_labels`` over a Gemini flash-lite model, so the dollars, tokens and seconds per call are what production pays
(the placement runs through the host ``generate`` adapter, see ``fast_model_labels``). API priced (fractions of a cent per case): one
case, then the set, with ``--max-calls``."""
PAID = True
from quest_ai_runner.adapters import card_metadata_generator, context_card_updater
from label_context_cards import arms
import live_arm


def build(meter):
    assert card_metadata_generator.CardMetadataGenerator and context_card_updater.categorize_files_with_llm   # the shipped code is the measured code
    provider, model = live_arm.build(meter, "fast_api")
    return lambda case: arms.run_retrieve(case, provider, model) if case["kind"] == "retrieve" else arms.run_place(case, provider, model)
