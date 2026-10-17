"""CURRENT: the live configuration path. ``RunnerConfig.from_file`` for the file alone, ``cli._config_from_env``
(which runs ``apply_config_environment`` and ``apply_file_defaults``) for the layered startup path, and
``core_settings_from_env`` for tuning values. The process environment is the only thing faked, and only by
scrubbing it. Zero model calls."""
import os
import tempfile
from pathlib import Path

from common import hermetic_env
from quest_ai_runner.cli_config import _config_from_env
from quest_ai_runner.config import ConfigFileError, RunnerConfig
from quest_ai_runner.config_core_settings import core_settings_from_env

CORE_FIELDS = ("read_op_timeout_seconds", "context_assembly_timeout_seconds", "verify_context_max_chars",
               "note_floor_relevance_gated", "deep_prompt_token_budget", "deep_timeout_seconds",
               "deep_review_interval_seconds")


def pick(cfg, names):
    return {n: getattr(cfg, n) for n in names}


def file_fields(args):
    with tempfile.TemporaryDirectory(prefix="qar_cfg_") as root:
        path = Path(root) / "lane.toml"
        path.write_text(args["toml"])
        try:
            cfg = RunnerConfig.from_file(path)
        except ConfigFileError as e:
            return {"error": type(e).__name__, "fields": {}, "personas": None}
    return {"error": None, "fields": pick(cfg, args.get("fields", [])),
            "personas": cfg.personas.skills_root if cfg.personas else None}


def layered(args):
    with tempfile.TemporaryDirectory(prefix="qar_cfg_") as root:
        for name, text in args.get("files", {}).items():
            (Path(root) / name).write_text(text)
        path = Path(root) / "lane.toml"
        path.write_text(args["toml"].replace("{dir}", root))
        probe = args.get("probe", [])
        with hermetic_env(dict(args.get("process", {}), QAR_CONVERSATION_SEARCH="false")):
            try:
                cfg = _config_from_env(str(path))
            except ConfigFileError as e:
                return {"error": type(e).__name__, "fields": {}, "environ": {}}
            return {"error": None, "fields": pick(cfg, args.get("fields", [])),
                    "environ": {k: os.environ.get(k) for k in probe}}


def core_settings(args):
    settings = core_settings_from_env(args["env"])
    return {"settings": {n: getattr(settings, n) for n in CORE_FIELDS}}


KINDS = {"file_fields": file_fields, "layered": layered, "core_settings": core_settings}


def build(meter):  # noqa: ARG001
    def configure(kind, args):
        return KINDS[kind](args)
    return configure
