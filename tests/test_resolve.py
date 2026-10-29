"""Selection by name: core/resolve.py turns (solution_id, name) into the implementation the code declares.

Offline and fast. Covers the declarations (every one imports), the precedence rules, loud failure on a typo,
run-time registration, that core/resolve.py reads no environment itself, and the live selection sites (model
provider, search backend, vector store, deep runner) following the declarations rather than an if/elif.
"""
import dataclasses
import json
import re
from pathlib import Path

import pytest
from esoi import config as esoi_config
from esoi import declarations, runtime

from quest_ai_runner.core import resolve as resolve_mod
from quest_ai_runner.core.resolve import chosen_name, default_name, names, register, resolve, resolve_config

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = ROOT / "quest_ai_runner" / "esoi_selection.json"
CONFIG = esoi_config.load(ROOT)
DECLARED = declarations.scan(CONFIG, declarations.code_files(CONFIG))


@pytest.fixture(autouse=True)
def clean_registrations():
    def drop():
        scope = runtime.REGISTRY.get((resolve_mod.SCOPE, "search-the-web"))
        if scope is not None:
            scope.implementations.pop("mine", None)
        resolve_mod.CONFIG_REFS.clear()
    drop()
    yield
    drop()


def test_every_declared_implementation_imports():
    declared = sorted(DECLARED.implementations)
    assert len(declared) > 40
    for sid, name in declared:
        assert resolve(sid, name) is not None, f"{sid}/{name}"
        assert name in names(sid)


def test_default_is_used_when_nothing_asks_for_another():
    assert chosen_name("call-a-model") == default_name("call-a-model") == "multi_provider"
    assert resolve("call-a-model").__name__ == "MultiProvider"


def test_precedence_is_name_then_override_then_default():
    over = {"call-a-model": "gemini"}
    assert resolve("call-a-model", overrides=over).__name__ == "GeminiProvider"
    assert resolve("call-a-model", "openai", overrides=over).__name__ == "OpenAIProvider"
    assert resolve("call-a-model", overrides={}).__name__ == "MultiProvider"
    assert chosen_name("call-a-model", "  Anthropic ") == "anthropic"


def test_the_environment_and_a_context_block_select_below_a_name(monkeypatch):
    monkeypatch.setenv(runtime.env_name("call-a-model", resolve_mod.SCOPE), "gemini")
    assert resolve("call-a-model").__name__ == "GeminiProvider"
    assert resolve("call-a-model", "openai").__name__ == "OpenAIProvider"
    with runtime.override("call-a-model", "deepseek", scope=resolve_mod.SCOPE):
        assert resolve("call-a-model").__name__ == "DeepSeekProvider"


def test_unknown_names_fail_loudly_with_the_choices():
    with pytest.raises(ValueError, match="choices: .*anthropic"):
        resolve("call-a-model", "nope")
    with pytest.raises(ValueError, match="no implementation is selected.*call-a-model"):
        resolve("not-a-solution")


def test_a_config_class_comes_with_runners_that_take_one():
    assert dataclasses.is_dataclass(resolve_config("run-deep-work", "acp"))
    assert dataclasses.is_dataclass(resolve_config("run-deep-work", "subprocess"))
    assert resolve_config("run-deep-work", "fast_edit") is None


def test_register_adds_an_implementation_of_the_consumers_own():
    register("search-the-web", "mine", "quest_ai_runner.adapters.web_search_backends:BraveBackend")
    assert "mine" in names("search-the-web")
    assert resolve("search-the-web", "mine").__name__ == "BraveBackend"
    assert default_name("search-the-web") == "web_research"
    assert resolve("search-the-web", overrides={"search-the-web": "mine"}).__name__ == "BraveBackend"


def test_a_consumer_registration_replaces_the_librarys_name_even_when_the_library_loads_later():
    entry = runtime.entry_for("write-files", None, resolve_mod.SCOPE)
    saved = dict(entry.implementations)
    try:
        register("write-files", "files", "quest_ai_runner.adapters.web_search_backends:BraveBackend")
        assert resolve("write-files", "files").__name__ == "BraveBackend"
        import quest_ai_runner.adapters.files_writer  # noqa: F401  (the library's own declaration steps aside)
        assert resolve("write-files", "files").__name__ == "BraveBackend"
    finally:
        entry.implementations.clear()
        entry.implementations.update(saved)
        entry.replaced.discard("files")


def test_resolve_module_reads_no_environment():
    text = (ROOT / "quest_ai_runner" / "core" / "resolve.py").read_text()
    assert not re.search(r"os\.environ|os\.getenv|\bgetenv\(", text)


# --- the selection sites use the declarations ------------------------------------------------------

def test_model_provider_follows_the_declarations(monkeypatch):
    from quest_ai_runner.cli_config import _model_provider_from_env
    for var in ("OPENAI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("QAR_MODEL_BACKEND", "claude_cli")
    assert type(_model_provider_from_env()).__name__ == "ClaudeCliProvider"
    monkeypatch.setenv("QAR_MODEL_BACKEND", "nope")
    with pytest.raises(ValueError, match="Unknown QAR_MODEL_BACKEND: nope"):
        _model_provider_from_env()


def test_search_backend_class_comes_from_the_declarations():
    from quest_ai_runner.adapters.web_search_backends import BraveBackend, SerperBackend, select_search_backend
    assert isinstance(select_search_backend({"QAR_WEB_SEARCH_BACKEND": "serper", "SERPER_API_KEY": "k"}), SerperBackend)
    assert isinstance(select_search_backend({"QAR_WEB_SEARCH_BACKEND": "brave", "BRAVE_API_KEY": "k"}), BraveBackend)
    assert select_search_backend({"QAR_WEB_SEARCH_BACKEND": "serper"}) is None      # key missing
    assert select_search_backend({"QAR_WEB_SEARCH_BACKEND": "unknown", "SERPER_API_KEY": "k"}) is None


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


def test_vector_store_class_is_resolved_through_the_declarations():
    assert resolve("index-by-meaning").__name__ == "QdrantVectorStore"


# --- the project default is written once, in the records ------------------------------------------------

def test_every_declaration_has_a_record_and_the_default_is_one_of_them():
    registry = pytest.importorskip("esoi.registry").load(CONFIG)
    assert declarations.drift(CONFIG, registry, DECLARED) == []
    by_solution = {}
    for sid, name in DECLARED.implementations:
        by_solution.setdefault(sid, set()).add(name)
    for sid, declared_names in by_solution.items():
        assert default_name(sid) in declared_names, sid


def test_the_shipped_snapshot_matches_the_records():
    snapshot = runtime.snapshot(CONFIG)
    shipped = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert shipped == snapshot, "run: esoi select --snapshot quest_ai_runner/esoi_selection.json"


def test_an_installed_copy_without_the_records_still_has_its_defaults(tmp_path):
    """A package installed elsewhere (quest-backend installs QAR) has no esoi/ records: the snapshot beside the
    package gives the defaults and the module of each implementation, and the folders around an installed package are
    never searched for someone else's records."""
    import shutil
    import subprocess
    import sys
    site = tmp_path / "site-packages"
    shutil.copytree(ROOT / "quest_ai_runner", site / "quest_ai_runner",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    stray = tmp_path / "esoi.toml"   # an unrelated project's configuration, above the installed package
    stray.write_text('structure_dir = "nowhere"\n')
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    esoi_home = str(Path(runtime.__file__).resolve().parent.parent)
    hub_home = str(ROOT.parent / "ai-hub-of-hubs-structure")   # the required hub, as a sibling checkout (not pip-installed here)
    code = (
        "import quest_ai_runner.core.resolve as r\n"
        f"assert r.__file__.startswith({str(site)!r}), r.__file__\n"
        "assert r.default_name('call-a-model') == 'multi_provider'\n"
        "assert r.resolve('search-the-web').__name__ == 'WebResearchAdapter'\n"
        "assert r.resolve('cache-cards').__name__ == 'FileCardCache'\n"
        "assert r.resolve('run-deep-work', 'acp').__name__ == 'AcpDeepRunner'\n"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=elsewhere, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "PYTHONPATH": f"{site}:{esoi_home}:{hub_home}", "HOME": str(tmp_path)})
    assert done.returncode == 0, done.stderr
