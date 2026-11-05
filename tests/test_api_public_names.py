"""``quest_ai_runner.api`` is the import path the Quest backend uses; every name it lists must resolve, and no hub name is re-exported."""
import importlib

import pytest

import quest_ai_runner.api as api


@pytest.mark.parametrize("name", api.__all__)
def test_every_public_name_resolves_to_a_quest_ai_runner_definition(name):
    value = getattr(api, name)
    module = getattr(value, "__module__", None)
    if isinstance(value, type) or callable(value) and module:   # a constant has no home module to check
        assert module.startswith("quest_ai_runner"), f"{name} is the hub's: import it from hubofhubs.api"
        assert getattr(importlib.import_module(module), name, value) is value


def test_the_list_has_no_duplicates():
    assert len(api.__all__) == len(set(api.__all__))
