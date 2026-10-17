"""CURRENT: the live selection path. ``core.resolve`` (``resolve``, ``chosen_name``, ``default_name``,
``names``) over the live ``SELECTION`` table, and the real call sites that consume it:
``cli_config._model_provider_from_env``, ``config_resolvers.resolve_deep_runner`` and
``select_search_backend``. The shell environment is scrubbed (``hermetic_env``) and fake keys are set only
inside the block. Zero model calls."""
import importlib

from hermetic_env import hermetic_env
from quest_ai_runner.adapters.web_search_backends import select_search_backend
from quest_ai_runner.cli_config import _model_provider_from_env
from quest_ai_runner.config import RunnerConfig
from quest_ai_runner.config_resolvers import resolve_deep_runner
from quest_ai_runner.core.resolve import chosen_name, default_name, names, resolve
from quest_ai_runner.core.selection_table import SELECTION

FAKE_KEYS = {"ANTHROPIC_API_KEY": "k", "GOOGLE_API_KEY": "k", "OPENAI_API_KEY": "k", "DEEPSEEK_API_KEY": "k",
             "BRAVE_API_KEY": "k", "SERPER_API_KEY": "k", "TAVILY_API_KEY": "k"}


def outcome(call):
    try:
        got = call()
    except Exception as e:  # noqa: BLE001
        return {"cls": None, "error": type(e).__name__}
    return {"cls": getattr(got, "__name__", type(got).__name__) if got is not None else None, "error": None}


def by_name(args):
    return outcome(lambda: resolve(args["solution"], args["name"]))


def by_override(args):
    return outcome(lambda: resolve(args["solution"], args.get("name"), overrides=args["overrides"]))


def by_default(args):
    out = outcome(lambda: resolve(args["solution"]))
    out["default"] = default_name(args["solution"])
    return out


def typo(args):
    try:
        resolve(args["solution"], args["name"])
    except ValueError as e:
        known = names(args["solution"]) if args["solution"] in SELECTION else sorted(SELECTION)
        return {"error": "ValueError", "lists_choices": all(n in str(e) for n in known)}
    return {"error": None, "lists_choices": False}


def integrity(args):  # noqa: ARG001
    unresolvable, bad_defaults = [], []
    for sid, entry in SELECTION.items():
        if entry["default"] not in entry["implementations"]:
            bad_defaults.append(sid)
        for name in names(sid):
            try:
                if not callable(resolve(sid, name)):
                    unresolvable.append(f"{sid}/{name}")
            except Exception:  # noqa: BLE001
                unresolvable.append(f"{sid}/{name}")
    return {"unresolvable": unresolvable, "bad_defaults": bad_defaults, "solutions": len(SELECTION)}


def live_switch(args):
    site = args["site"]
    if site == "model_provider":
        with hermetic_env(dict(FAKE_KEYS, QAR_MODEL_BACKEND=args["backend"])):
            return outcome(_model_provider_from_env)
    if site == "deep_runner":
        with hermetic_env({"QAR_CLAUDE_PATH": "/bin/true"}):
            cfg = RunnerConfig(selections=args.get("selections", {}))
            return outcome(lambda: resolve_deep_runner(cfg, warn=False))
    if site == "search_backend":
        with hermetic_env(dict(FAKE_KEYS, QAR_WEB_SEARCH_BACKEND=args["backend"])):
            return outcome(select_search_backend)
    raise ValueError(site)


KINDS = {"by_name": by_name, "by_override": by_override, "by_default": by_default, "typo": typo,
         "integrity": integrity, "live_switch": live_switch}


def build(meter):  # noqa: ARG001
    def select(kind, args):
        return KINDS[kind](args)
    return select
