"""CURRENT: the real package surface, driven directly. Imports ``quest_ai_runner`` and looks names up, runs the
registrations' observable effects, calls ``load_client`` and ``load_config`` on a scrubbed environment, runs the
async bridge and signature probes, the reply-boundary strippers, the bootstrap-report builders and the vendored
Aider edit parser, and imports modules in a subprocess whose sockets raise. Zero model calls, zero network."""
import contextlib
import dataclasses
import importlib
import os
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone

import quest_ai_runner
from quest_ai_runner import bootstrap_email
from quest_ai_runner.core import async_bridge, scaffolding, signature_probe
from hubofhubs.act import editblock as aider_editblock

SEMVER = re.compile(r"^\d+\.\d+\.\d+([.-]\w+)?$")
NO_NETWORK = ("import socket\n"
              "def refuse(*a, **k):\n    raise OSError('network disabled')\n"
              "socket.socket.connect = refuse\nsocket.create_connection = refuse\nsocket.getaddrinfo = refuse\n"
              "import importlib\nimportlib.import_module({module!r})\n")


@dataclasses.dataclass
class WithHiddenField:
    shown: int
    other: str = "x"
    hidden: int = dataclasses.field(default=0, init=False)


class TwoArgs:
    def __init__(self, x, y=1):
        self.x = x

    def method(self, a, b=2, *, c=3):
        return a


class AnyArgs:
    def __init__(self, **options):
        self.options = options


def plain(a, b, *, c=1):
    return a


def kwargs_fn(a, **rest):
    return a


KEYWORD_FIXTURES = {"plain": plain, "kwargs": kwargs_fn, "builtin": len, "bound_method": TwoArgs(1).method}
FIELD_FIXTURES = {"dataclass_with_init_false": WithHiddenField, "plain_init": TwoArgs, "init_kwargs": AnyArgs}


@contextlib.contextmanager
def scrubbed_env(extra):
    """A process environment holding no QAR_/QUEST_ variable except ``extra``; restored afterwards."""
    saved = dict(os.environ)
    try:
        for key in list(os.environ):
            if key.startswith(("QAR_", "QUEST_")):
                del os.environ[key]
        os.environ.update(extra)
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def front_door(args):
    with scrubbed_env(args.get("env", {})), tempfile.TemporaryDirectory() as tmp:
        try:
            if args["door"] == "load_client":
                return {"client": type(quest_ai_runner.load_client()).__name__}
            if args["door"] == "load_config":
                return {"base_url": quest_ai_runner.load_config().quest_base_url}
            if args["door"] == "load_config_bad_file":
                path = os.path.join(tmp, "bad.toml")
                with open(path, "w") as fh:
                    fh.write("this is = = not toml [")
                quest_ai_runner.load_config(path)
        except Exception as e:  # noqa: BLE001 - the exception type is the observation
            return {"raised": type(e).__name__}
        return {"raised": None}


def hook(args):
    from quest_ai_runner.adapters.claude_cli_provider import cli_model
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.core import goal_runner_contract, tools
    from quest_ai_runner.runner import config_wiring
    if args["hook"] == "translator":
        return {"is_cli_model": getattr(goal_runner_contract, "_cli_model_translator") is cli_model}
    if args["hook"] == "client_factory":
        client = tools.quest_client_from_env({"QUEST_BASE_URL": "http://quest.invalid", "QUEST_API_KEY": "synthetic"})
        return {"client": type(client).__name__}
    registered = []
    for name in ("derive_capabilities", "resolve_tool_registry"):
        try:
            getattr(config_wiring, name)(RunnerConfig())
            registered.append(name)
        except RuntimeError:
            pass
    return {"registered": sorted(registered)}


def loop(args):  # noqa: ARG001
    async def where():
        return threading.current_thread().name

    async def boom():
        raise ValueError("synthetic")

    first, second = async_bridge.run_coro_sync(where()), async_bridge.run_coro_sync(where())
    try:
        async_bridge.run_coro_sync(boom())
        propagates = False
    except ValueError:
        propagates = True
    return {"same_thread": first == second, "thread": first, "propagates": propagates}


def detached(args):
    done, seen = threading.Event(), {}

    def work():
        seen["thread"] = threading.current_thread().name
        done.set()
        if args.get("fail"):
            raise RuntimeError("synthetic failure")

    try:
        future = async_bridge.run_detached(work)
        done.wait(5)
        if future is not None:
            try:
                future.result(5)
            except Exception:  # noqa: BLE001
                return {"ran": done.is_set(), "raised": True}
        return {"ran": done.is_set() and seen.get("thread") != threading.current_thread().name, "raised": False}
    except Exception:  # noqa: BLE001
        return {"ran": False, "raised": True}


def email_due(args):
    form = args.get("form", "iso")
    if form == "missing":
        state = {}
    elif form == "empty":
        state = {"last_email_sent": ""}
    elif form == "garbage":
        state = {"last_email_sent": "last tuesday"}
    else:
        when = datetime.now(timezone.utc) - timedelta(days=args["days_ago"])
        state = {"last_email_sent": when.replace(tzinfo=None).isoformat() if form == "naive" else when.isoformat()}
    return {"due": bootstrap_email.should_send_monthly_email(state, args.get("months", 1))}


def edit_blocks(args):
    if args["fn"] == "search_replace":
        return {"blocks": [list(b) for b in aider_editblock.find_original_update_blocks(args["content"])]}
    return {"blocks": [list(b) for b in aider_editblock.find_whole_file_blocks(args["content"], valid_fnames=args.get("valid"))]}


def offline_import(args):
    code = NO_NETWORK.format(module=args["module"])
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                          env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)))
    return {"imported": done.returncode == 0, "stderr": done.stderr[-200:]}


def build(meter):  # noqa: ARG001
    def surface(kind, args):
        if kind == "name":
            module = importlib.import_module(args["module"])
            obj = getattr(module, args["name"], None)
            return {"found": obj is not None, "callable": callable(obj)}
        if kind == "version":
            version = quest_ai_runner.__version__
            return {"semver": bool(SEMVER.match(version)), "exported": "__version__" in quest_ai_runner.__all__}
        if kind == "hook":
            return hook(args)
        if kind == "front_door":
            return front_door(args)
        if kind == "loop":
            return loop(args)
        if kind == "detached":
            return detached(args)
        if kind == "keywords":
            names = signature_probe.accepted_keywords(KEYWORD_FIXTURES[args["fixture"]])
            return {"names": None if names is None else sorted(names)}
        if kind == "fields":
            names = signature_probe.accepted_fields(FIELD_FIXTURES[args["fixture"]])
            return {"names": None if names is None else sorted(names)}
        if kind == "filter":
            allowed = None if args["allowed"] is None else set(args["allowed"])
            return {"kept": signature_probe.filter_keywords(args["kwargs"], allowed, "synthetic")}
        if kind == "scrub":
            return {"text": getattr(scaffolding, args["fn"])(args["text"])}
        if kind == "email_due":
            return email_due(args)
        if kind == "email_body":
            return {"text": bootstrap_email.generate_bootstrap_email_body(**args)}
        if kind == "offline_import":
            return offline_import(args)
        if kind == "edit":
            result = aider_editblock.apply_edit(args["content"], args["before"], args["after"])
            return {"text": result or "", "ok": result is not None}
        if kind == "edit_blocks":
            return edit_blocks(args)
        raise ValueError(kind)
    return surface
