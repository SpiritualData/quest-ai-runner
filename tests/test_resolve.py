"""Selection as data: core/resolve.py turns (solution_id, name) into an implementation from the table.

Offline and fast. Covers the table itself (every entry imports), the precedence rules, loud failure on a
typo, run-time registration, that core/resolve.py reads no environment, and the live selection sites
(model provider, search backend, vector store, deep runner) following the table rather than an if/elif.
"""
import dataclasses
import re
from pathlib import Path

import pytest

from quest_ai_runner.core import resolve as resolve_mod
from quest_ai_runner.core.resolve import chosen_name, default_name, names, register, resolve, resolve_config
from quest_ai_runner.core.selection_table import SELECTION

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_registrations():
    resolve_mod._EXTRA.clear()
    resolve_mod._EXTRA_DEFAULT.clear()
    yield
    resolve_mod._EXTRA.clear()
    resolve_mod._EXTRA_DEFAULT.clear()


def test_every_table_entry_imports():
    for sid in SELECTION:
        for name in names(sid):
            assert resolve(sid, name) is not None, f"{sid}/{name}"


def test_default_is_used_when_nothing_asks_for_another():
    assert chosen_name("call-a-model") == default_name("call-a-model") == "multi_provider"
    assert resolve("call-a-model").__name__ == "MultiProvider"


def test_precedence_is_name_then_override_then_default():
    over = {"call-a-model": "gemini"}
    assert resolve("call-a-model", overrides=over).__name__ == "GeminiProvider"
    assert resolve("call-a-model", "openai", overrides=over).__name__ == "OpenAIProvider"
    assert resolve("call-a-model", overrides={}).__name__ == "MultiProvider"
    assert chosen_name("call-a-model", "  Anthropic ") == "anthropic"


def test_unknown_names_fail_loudly_with_the_choices():
    with pytest.raises(ValueError, match="choices: .*anthropic"):
        resolve("call-a-model", "nope")
    with pytest.raises(ValueError, match="no selection table entry"):
        resolve("not-a-solution")


def test_a_config_class_comes_with_runners_that_take_one():
    assert dataclasses.is_dataclass(resolve_config("run-deep-work", "acp"))
    assert resolve_config("run-deep-work", "fast_edit") is None


def test_register_adds_an_implementation_and_can_make_it_the_default():
    register("search-the-web", "mine", "quest_ai_runner.adapters.web_search_backends:BraveBackend")
    assert "mine" in names("search-the-web")
    assert resolve("search-the-web", "mine").__name__ == "BraveBackend"
    assert default_name("search-the-web") == "web_research"
    register("search-the-web", "mine", "quest_ai_runner.adapters.web_search_backends:BraveBackend", default=True)
    assert resolve("search-the-web").__name__ == "BraveBackend"
    with pytest.raises(ValueError):
        register("not-a-solution", "x", "a.b:C")


def test_resolve_module_reads_no_environment():
    for f in ("resolve.py", "selection_table.py"):
        text = (ROOT / "quest_ai_runner" / "core" / f).read_text()
        assert not re.search(r"os\.environ|os\.getenv|\bgetenv\(", text), f


# --- the selection sites use the table -------------------------------------------------------------

def test_model_provider_follows_the_table(monkeypatch):
    from quest_ai_runner.cli_config import _model_provider_from_env
    for var in ("OPENAI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("QAR_MODEL_BACKEND", "claude_cli")
    assert type(_model_provider_from_env()).__name__ == "ClaudeCliProvider"
    monkeypatch.setenv("QAR_MODEL_BACKEND", "nope")
    with pytest.raises(ValueError, match="Unknown QAR_MODEL_BACKEND: nope"):
        _model_provider_from_env()


def test_search_backend_class_comes_from_the_table():
    from quest_ai_runner.adapters.web_search_backends import BraveBackend, SerperBackend, select_search_backend
    assert isinstance(select_search_backend({"QAR_WEB_SEARCH_BACKEND": "serper", "SERPER_API_KEY": "k"}), SerperBackend)
    assert isinstance(select_search_backend({"QAR_WEB_SEARCH_BACKEND": "brave", "BRAVE_API_KEY": "k"}), BraveBackend)
    assert select_search_backend({"QAR_WEB_SEARCH_BACKEND": "serper"}) is None      # key missing
    assert select_search_backend({"QAR_WEB_SEARCH_BACKEND": "unknown", "SERPER_API_KEY": "k"}) is None


def test_switching_a_search_backend_is_a_table_change(monkeypatch):
    """Point the table name at another class: the selection site follows with no code change."""
    from quest_ai_runner.adapters.web_search_backends import BraveBackend, select_search_backend
    register("search-the-web", "serper", "quest_ai_runner.adapters.web_search_backends:BraveBackend")
    assert isinstance(select_search_backend({"QAR_WEB_SEARCH_BACKEND": "serper", "SERPER_API_KEY": "k"}), BraveBackend)


def test_deep_runner_default_and_override(monkeypatch, tmp_path):
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.config_resolvers import resolve_deep_runner
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("QAR_CLAUDE_PATH", str(fake))
    cfg = RunnerConfig(corpus_root=str(tmp_path))
    assert type(resolve_deep_runner(cfg)).__name__ == "SubprocessGoalRunner"
    cfg = RunnerConfig(corpus_root=str(tmp_path), selections={"run-deep-work": "acp"})
    assert type(resolve_deep_runner(cfg)).__name__ == "AcpDeepRunner"
    # a ladder rung is not a standalone worker: reported, not raised
    cfg = RunnerConfig(corpus_root=str(tmp_path), selections={"run-deep-work": "fast_edit"})
    assert resolve_deep_runner(cfg, warn=False) is None


def test_selections_can_come_from_a_config_file(tmp_path):
    from quest_ai_runner.config import RunnerConfig
    p = tmp_path / "lane.toml"
    p.write_text('[selections]\n"run-deep-work" = "acp"\n"index-by-meaning" = "qdrant"\n')
    cfg = RunnerConfig.from_file(str(p))
    assert cfg.selections == {"run-deep-work": "acp", "index-by-meaning": "qdrant"}


def test_vector_store_class_is_resolved_through_the_table(monkeypatch):
    import quest_ai_runner.adapters.qdrant_vector_store as mod
    sentinel = type("Sentinel", (), {})
    monkeypatch.setattr(mod, "QdrantVectorStore", sentinel)
    assert resolve("index-by-meaning") is sentinel


# --- the project default is written once, in the records ------------------------------------------------

def test_the_table_holds_no_default_of_its_own():
    assert [sid for sid, entry in SELECTION.items() if "default" in entry] == []


def test_the_shipped_snapshot_matches_the_records():
    esoi_select = pytest.importorskip("esoi.runtime")
    import esoi.config
    snapshot = esoi_select.snapshot(esoi.config.load(ROOT))
    shipped = resolve_mod.json.loads(resolve_mod.SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert shipped == snapshot, "run: esoi select --snapshot quest_ai_runner/esoi_selection.json"
    for sid in SELECTION:
        assert default_name(sid) == snapshot[sid]["default"]


def test_an_installed_copy_without_the_records_or_esoi_still_has_its_defaults(tmp_path):
    """A package installed elsewhere (quest-backend installs QAR) has neither esoi/ nor the esoi package."""
    import shutil
    import subprocess
    import sys
    site = tmp_path / "site-packages"
    shutil.copytree(ROOT / "quest_ai_runner", site / "quest_ai_runner",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    code = (
        "import sys; sys.modules['esoi'] = None\n"
        "import quest_ai_runner.core.resolve as r\n"
        f"assert r.__file__.startswith({str(site)!r}), r.__file__\n"
        "from quest_ai_runner.core.selection_table import SELECTION\n"
        "bad = [s for s in SELECTION if not r.default_name(s)]\n"
        "assert not bad, bad\n"
        "assert r.default_name('call-a-model') == 'multi_provider'\n"
        "assert r.resolve('search-the-web').__name__ == 'WebResearchAdapter'\n"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=elsewhere, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(site), "HOME": str(tmp_path)})
    assert done.returncode == 0, done.stderr
    assert not (site / "esoi").exists() and not (tmp_path / "esoi").exists()
