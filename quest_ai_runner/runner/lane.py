"""lane — the shared ``--check`` / ``--once`` / loop-forever entry point every executor lane needs.

Three separate consumers of this library each hand-rolled the same ~40 lines of glue around
``Poller`` (the argv shape, logging setup, ``.env`` loading, "degrade instead of crash on an
incomplete config" behavior, and the actual check/once/loop calls). Two of them noticed the
duplication and extracted a shared copy of it — but OUTSIDE this library, so the third consumer had
nowhere to find it and wrote a fourth copy. That is exactly the failure hard rule #3 (see
``CLAUDE.md``) now names: this is generic, reusable-by-a-second-consumer plumbing, so it belongs
here, not in a lane's own file. See ``CHANGELOG.md`` (Unreleased) for the migration this closes.

A consumer builds its own ``RunnerConfig`` (credentials, adapters, persona, corpus — everything
genuinely specific to that lane) and calls ``run_lane`` with it:

    def main(argv=None) -> int:
        return run_lane(argv, prog="my-lane", description="...", lane_label="my-lane",
                        log_name="my-lane", build_config=build_config)

That is the whole entry point. See ``docs/tutorial-your-first-lane.md`` for a walkthrough and
``examples/minimal_lane.py`` for a runnable one.
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

from ..config import RunnerConfig

__all__ = ["run_lane", "load_env_file", "install_local_time_due_gate", "build_arg_parser"]

PathLike = Union[str, "os.PathLike[str]"]


def load_env_file(path: PathLike) -> None:
    """Load simple ``KEY=VALUE`` lines from an ``.env`` file into ``os.environ`` (no extra deps).

    Already-set process env vars win (so a systemd unit's own ``Environment=`` still overrides),
    matching the convention every lane on this pattern has always used. A missing file is a
    silent no-op — plenty of lanes read everything from the process environment and pass no
    ``env_file`` to ``run_lane`` at all.
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.split("#", 1)[0].strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def install_local_time_due_gate(log: logging.Logger) -> None:
    """No-op inside this library — kept only so a caller that names it explicitly keeps working.

    Before this driver moved into the library, a lane-level fallback lived here: it monkey-patched
    ``QuestClient.discover_due`` to re-filter tasks by LOCAL wall-clock time, because the backend's
    ``due_before`` filter is date-granular and a task scheduled for, say, 06:30 tomorrow would
    otherwise go "due" the moment UTC rolls over — 17:00 today, west of UTC. The fallback stood
    down automatically whenever ``quest_ai_runner.runner.poller`` already carried its own
    ``_due_now_locally`` filter, which is the real, tested fix and now ships unconditionally with
    every ``Poller`` (see ``runner/poller.py`` and ``runner/local_time.py``).

    Now that this driver lives IN the library, that stand-down condition can never be false: this
    module and ``runner/poller.py`` are always the same install, so ``_due_now_locally`` is always
    present by construction. Carrying the monkey-patch here would be dead code masquerading as a
    safety net — worse than deleting it outright, because a future reader could believe it still
    does something. So the behavior itself was dropped (see ``CHANGELOG.md``, Unreleased); this
    function stays only as a harmless, logged no-op for a caller that still names it by hand.
    """
    log.debug("local-time due gate: handled natively by quest_ai_runner.runner.poller "
              "(_due_now_locally / runner/local_time.py); no lane-level fallback is needed")


def build_arg_parser(prog: str, description: str) -> argparse.ArgumentParser:
    """The shared ``--once`` / ``--check`` / ``-v`` CLI shape every lane exposes."""
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("--once", action="store_true", help="one scan then exit")
    parser.add_argument("--check", action="store_true", help="validate config + key, then exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def run_lane(
    argv,
    *,
    prog: str,
    description: str,
    lane_label: str,
    log_name: str,
    build_config: Callable[[], RunnerConfig],
    env_file: Optional[PathLike] = None,
    state_path: Optional[PathLike] = None,
    not_configured_keywords: Sequence[str] = ("key", "url"),
) -> int:
    """The shared ``--check`` / ``--once`` / loop-forever driver every executor lane's ``main()``
    should be nothing more than a call to.

    ``build_config`` is a zero-arg callable supplied by the lane: it reads its own env/config,
    wires its own adapters/persona/preamble, and returns a fully-built ``RunnerConfig``. Everything
    lane-specific stays there; this function only knows the generic ``RunnerConfig`` / ``Poller``
    shape. It is called AFTER ``env_file`` is loaded (when one is given), so an ``os.getenv()``
    inside ``build_config`` sees that file's values already in ``os.environ`` — the same ordering
    every lane on this pattern has relied on.

    ``env_file``: optional path to a ``KEY=VALUE`` file loaded via :func:`load_env_file` before
    ``build_config`` runs. Omit it for a lane that reads only the process environment (a systemd
    unit's own ``Environment=``, a container's env, ...).

    ``state_path``: where the exactly-once signature dedup store lives. Omit it to use
    ``QAR_STATE_PATH`` (falling back to ``qar_state.json`` in the working directory) — the same
    default the stock ``quest-ai-runner`` console entry point uses, so a lane needs to name this
    explicitly only when it wants a state file somewhere else.

    ``lane_label`` (e.g. ``"personal"`` / ``"cantr"``) drives every shared log message
    (``"<label> lane not fully configured: ..."``, ``"<label> key OK -> ..."``,
    ``"<label> key check failed: ..."``, ``"starting <label> quest-ai-runner loop ..."``), so two
    lanes' logs read distinctly with no per-lane string duplication needed here.

    ``not_configured_keywords`` controls which ``validate()`` problems mean "degrade instead of
    crash" (exit 0) versus letting the lane run with a partially-invalid config: a lane whose
    discovery is team-scoped might add ``"team"`` to the default ``("key", "url")`` so an unset
    team id is also treated as an early, not-yet-provisioned misconfiguration rather than a crash.
    """
    parser = build_arg_parser(prog, description)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger(log_name)

    if env_file is not None:
        load_env_file(env_file)
    # Generous context-assembly budget: the library default (5s) times out on a real corpus and
    # drops turn-start context ("Context assembly timed out"); 15s keeps it. setdefault so a
    # lane's own .env or its systemd unit can still override.
    os.environ.setdefault("QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS", "15")
    # A lane running without the optional [qdrant] extra installed should not log "Qdrant open
    # failed" on every scan; setdefault so a lane that DOES want the vector backend can still ask
    # for it via its own env/.env.
    os.environ.setdefault("QAR_VECTOR_BACKEND", "none")
    from .poller import Poller

    # Must run before any Poller exists: see the function's own docstring for why it is a no-op now.
    install_local_time_due_gate(log)

    cfg = build_config()
    problems = cfg.validate()
    if problems:
        for p in problems:
            log.info("%s lane not fully configured: %s", lane_label, p)
        # Degrade visibly: don't crash the service while credentials are still being provisioned.
        if any(any(kw in p for kw in not_configured_keywords) for p in problems):
            return 0

    resolved_state_path = Path(state_path) if state_path is not None \
        else Path(os.getenv("QAR_STATE_PATH", "qar_state.json"))
    resolved_state_path.parent.mkdir(parents=True, exist_ok=True)
    poller = Poller(cfg, state_path=str(resolved_state_path))

    if args.check:
        try:
            log.info("%s key OK -> %s", lane_label, poller.client.whoami())
            return 0
        except Exception as e:  # noqa: BLE001
            log.error("%s key check failed: %s", lane_label, e)
            return 1

    if args.once:
        handled = poller.run_once()
        log.info("handled %d task(s): %s", len(handled), handled)
        return 0

    log.info("starting %s quest-ai-runner loop (env_id=%s, interval=%ss)",
             lane_label, cfg.env_id, int(cfg.poll_interval_seconds))
    poller.run_forever()
    return 0
