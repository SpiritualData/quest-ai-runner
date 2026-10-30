"""QAR reads the card store's environment variables and hands them to the hub as arguments; the names and defaults are unchanged."""
import pytest

from quest_ai_runner.adapters import card_store_settings as settings

VARIABLES = ("QAR_MAX_CARDS", "QAR_BOOTSTRAP_WORKERS", "QAR_MAX_PARALLEL", "QAR_BOOTSTRAP_MAX_FILES", "QAR_BOOTSTRAP_MAX_CARDS",
             "QAR_BOOTSTRAP_MAX_CARDS_PER_FOLDER", "QAR_SKIP_NESTED_REPOS", "QAR_FOLDER_REVIEW", "QAR_PRUNE_DEAD_CARDS")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_the_defaults_are_the_ones_the_environment_always_gave():
    assert settings.max_cards_from_env() == 1000
    assert settings.bootstrap_workers() == 8
    assert settings.bootstrap_max_files() is None and settings.bootstrap_max_cards() is None
    assert settings.max_cards_per_folder() == 8
    assert settings.skip_nested_repos() and settings.folder_review_enabled() and settings.prune_dead_cards()


def test_the_card_limit_reads_qar_max_cards(monkeypatch):
    monkeypatch.setenv("QAR_MAX_CARDS", "250")
    assert settings.max_cards_from_env() == 250
    monkeypatch.setenv("QAR_MAX_CARDS", "unlimited")
    assert settings.max_cards_from_env() == 0
    assert settings.max_cards_from_env({"QAR_MAX_CARDS": "7"}) == 7


def test_the_workers_follow_the_stage_setting_then_the_deployment_budget(monkeypatch):
    monkeypatch.setenv("QAR_MAX_PARALLEL", "3")
    assert settings.bootstrap_workers() == 3
    monkeypatch.setenv("QAR_BOOTSTRAP_WORKERS", "2")
    assert settings.bootstrap_workers() == 2


def test_the_bootstrap_ceilings_and_the_per_folder_rule(monkeypatch):
    monkeypatch.setenv("QAR_BOOTSTRAP_MAX_FILES", "500")
    monkeypatch.setenv("QAR_BOOTSTRAP_MAX_CARDS", "40")
    monkeypatch.setenv("QAR_BOOTSTRAP_MAX_CARDS_PER_FOLDER", "0")
    assert (settings.bootstrap_max_files(), settings.bootstrap_max_cards(), settings.max_cards_per_folder()) == (500, 40, 0)


@pytest.mark.parametrize("name,reader", [("QAR_SKIP_NESTED_REPOS", "skip_nested_repos"), ("QAR_FOLDER_REVIEW", "folder_review_enabled"),
                                         ("QAR_PRUNE_DEAD_CARDS", "prune_dead_cards")])
def test_each_switch_turns_off_with_zero_false_or_no(monkeypatch, name, reader):
    for off in ("0", "false", "No"):
        monkeypatch.setenv(name, off)
        assert getattr(settings, reader)() is False
    monkeypatch.setenv(name, "1")
    assert getattr(settings, reader)() is True


def test_the_store_folder_name_is_qars():
    assert settings.STORE_DIR_NAME == ".quest-context"
