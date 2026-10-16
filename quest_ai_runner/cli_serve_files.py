"""The `serve-files` subcommand: run the read-only file server for one directory tree.

Split out of ``cli.py`` by definition; that module re-exports ``_cmd_serve_files``. The token is
read from the environment only (never from the command line, where ``ps`` would show it), and the
command refuses to start without one.
"""
from __future__ import annotations

import os

from .adapters.files_server import MIN_TOKEN_LENGTH, serve

DEFAULT_PORT = 8765


def _cmd_serve_files(args, log) -> int:
    """Run `serve-files`; returns the process exit code (1 when it cannot start safely)."""
    root = (getattr(args, "root", None) or os.getenv("QAR_FILES_ROOT") or "").strip()
    token = (os.getenv("QAR_FILES_TOKEN") or "").strip()
    host = (getattr(args, "host", None) or os.getenv("QAR_FILES_HOST") or "127.0.0.1").strip()
    port_text = str(getattr(args, "port", None) or os.getenv("QAR_FILES_PORT") or DEFAULT_PORT)
    if not root:
        log.error("serve-files needs a directory to serve: pass --root or set QAR_FILES_ROOT")
        return 1
    if len(token) < MIN_TOKEN_LENGTH:
        log.error("serve-files refuses to start without an access token: set QAR_FILES_TOKEN "
                  "to a random value of at least %d characters", MIN_TOKEN_LENGTH)
        return 1
    try:
        port = int(port_text)
    except ValueError:
        log.error("serve-files: %r is not a valid port", port_text)
        return 1
    deny = [d for d in (os.getenv("QAR_FILES_DENY") or "").split(",") if d.strip()]
    allow_dot = [d for d in (os.getenv("QAR_FILES_ALLOW_DOT") or "").split(",") if d.strip()]
    from .adapters.google_link_reader import build_link_reader_from_env
    links = build_link_reader_from_env()
    if links is not None:
        log.info("serve-files: private Google link reading is on")
    try:
        serve(root, token, host=host, port=port, deny=deny, allow_dot=allow_dot, links=links)
    except (ValueError, OSError) as exc:
        log.error("serve-files could not start: %s", exc)
        return 1
    return 0
