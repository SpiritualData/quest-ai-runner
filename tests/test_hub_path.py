"""A virtualenv built before hubofhubs was required still starts: the sibling checkout is found, or the error says what to run."""
import sys

import pytest

from quest_ai_runner import hub_path


def layout(tmp_path, with_hub):
    package_file = tmp_path / "launch_code" / "quest-ai-runner" / "quest_ai_runner" / "hub_path.py"
    package_file.parent.mkdir(parents=True)
    package_file.write_text("")
    if with_hub:
        hub = tmp_path / "launch_code" / "ai-hub-of-hubs-structure" / "hubofhubs"
        hub.mkdir(parents=True)
        (hub / "__init__.py").write_text("")
    return package_file


def test_the_sibling_checkout_is_found_beside_the_repository(tmp_path):
    assert hub_path.sibling_checkout(str(layout(tmp_path, True))) == tmp_path / "launch_code" / "ai-hub-of-hubs-structure"
    assert hub_path.sibling_checkout(str(layout(tmp_path / "other", False))) is None


def test_an_environment_without_the_package_gets_the_sibling_checkout_appended(tmp_path, monkeypatch):
    package_file = layout(tmp_path, True)
    monkeypatch.setattr(hub_path.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    hub_path.ensure_hub(str(package_file))
    assert sys.path[-1] == str(tmp_path / "launch_code" / "ai-hub-of-hubs-structure")


def test_an_installed_package_is_left_alone(tmp_path, monkeypatch):
    before = list(sys.path)
    monkeypatch.setattr(hub_path.importlib.util, "find_spec", lambda name: object())
    hub_path.ensure_hub(str(layout(tmp_path, True)))
    assert sys.path == before


def test_no_package_and_no_checkout_says_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_path.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ModuleNotFoundError) as caught:
        hub_path.ensure_hub(str(layout(tmp_path, False)))
    assert caught.value.name == "hubofhubs" and hub_path.HUB_REQUIREMENT in str(caught.value) and "—" not in str(caught.value)
