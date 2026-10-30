"""``core.adapters`` is the import path the Quest backend uses; every name it re-exports must keep resolving."""
import importlib

import pytest

PUBLIC_NAMES = {
    "hubofhubs.ports.context": ["AssembledContext", "ConversationContext", "ConversationStore"],
    "hubofhubs.ports.deep": [
        "DeepResult", "DeepRunnerBase", "FUTURE_CONTEXT_VIA_FIELD", "FUTURE_CONTEXT_VIA_OUTPUT", "ProgressEvent",
        "WRITE_SURFACE_OPERATIONS", "runner_starts_background_work", "runner_uses_deep_model",
    ],
    "hubofhubs.ports.events": ["EVENT_EXEC"],
    "quest_ai_runner.core.adapters_bases": ["EscalationSinkBase", "GuidanceProviderBase", "ModelProviderBase", "RetrievalAdapterBase"],
    "quest_ai_runner.core.adapters_guidance": ["GuidanceCard"],
    "quest_ai_runner.core.adapters_values": ["Escalation", "Observation"],
}


@pytest.mark.parametrize("module,name", [(m, n) for m, names in PUBLIC_NAMES.items() for n in names])
def test_the_public_name_is_the_one_its_defining_module_holds(module, name):
    public = importlib.import_module("quest_ai_runner.core.adapters")
    assert getattr(public, name) is getattr(importlib.import_module(module), name)
