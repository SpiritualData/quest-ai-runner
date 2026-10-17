"""BASELINE: the first config loader a lane would write. ``tomllib`` plus ``RunnerConfig(**known keys)``: an
unknown key is silently dropped, the file always wins over an environment variable, ``env_*`` tables are not
supported, a bad number raises, and a malformed tuning value crashes startup. Zero model calls."""
import dataclasses
import tomllib

from quest_ai_runner.config import RunnerConfig
from quest_ai_runner.core.runtime_settings import CoreSettings

CORE_FIELDS = ("read_op_timeout_seconds", "context_assembly_timeout_seconds", "verify_context_max_chars",
               "note_floor_relevance_gated", "deep_prompt_token_budget", "deep_timeout_seconds",
               "deep_review_interval_seconds")


def load(text):
    known = {f.name for f in dataclasses.fields(RunnerConfig)}
    data = {k: v for k, v in tomllib.loads(text).items() if k in known and k != "personas"}
    return RunnerConfig(**data)


def file_fields(args):
    try:
        cfg = load(args["toml"])
    except Exception as e:  # noqa: BLE001
        return {"error": type(e).__name__, "fields": {}, "personas": None}
    return {"error": None, "fields": {n: getattr(cfg, n) for n in args.get("fields", [])}, "personas": None}


def layered(args):
    out = file_fields(args)
    return {"error": out["error"], "fields": out["fields"], "environ": {k: args.get("process", {}).get(k) for k in args.get("probe", [])}}


def core_settings(args):
    env = args["env"]
    d = CoreSettings()
    values = {n: getattr(d, n) for n in CORE_FIELDS}
    for n, var in (("read_op_timeout_seconds", "QAR_READ_OP_TIMEOUT_SECONDS"),
                   ("context_assembly_timeout_seconds", "QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS"),
                   ("deep_timeout_seconds", "QAR_DEEP_TIMEOUT_SECONDS")):
        if var in env:
            values[n] = float(env[var])
    if "QAR_VERIFY_CONTEXT_MAX_CHARS" in env:
        values["verify_context_max_chars"] = int(env["QAR_VERIFY_CONTEXT_MAX_CHARS"])
    if "QAR_DEEP_PROMPT_TOKEN_BUDGET" in env:
        values["deep_prompt_token_budget"] = int(env["QAR_DEEP_PROMPT_TOKEN_BUDGET"])
    return {"settings": values}


def build(meter):  # noqa: ARG001
    kinds = {"file_fields": file_fields, "layered": layered, "core_settings": core_settings}

    def configure(kind, args):
        return kinds[kind](args)
    return configure
