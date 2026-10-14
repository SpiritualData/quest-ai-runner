"""Building the optional context sources (Drive comments, inbound mail, Notion, Google Chat) from config.

Split out of ``config.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional
from pathlib import Path

from .config_support import _log


def _load_json_mapping(path: str, what: str) -> Dict[str, Any]:
    """A ``{key: value}`` JSON file, or {} when it is absent or unusable. Never raises."""
    p = Path(path)
    if not p.exists():
        _log.info("config %s: %s does not exist yet", what, path)
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        _log.warning("config %s: %s is not readable JSON (%s)", what, path, e)
        return {}
    if not isinstance(data, dict):
        _log.warning("config %s: %s must hold a JSON object", what, path)
        return {}
    return data


def _build_drive_comments(auth: Dict[str, Any]) -> Any:
    """A ``DriveComments`` client from ``{service_account_file, subject, scopes}``, or None.

    Read+write scope by default (``COMMENT_WRITE_SCOPES``): reading a person's question without
    being able to answer it in the document is what makes them stop asking there.
    """
    try:
        from .adapters.drive_comments import COMMENT_WRITE_SCOPES, DriveComments
        from .adapters.google_chat_adapter import service_account_token_provider
    except Exception as e:  # noqa: BLE001 -- an optional channel never blocks a lane starting
        _log.warning("drive_comments_auth: channel unavailable (%s)", e)
        return None

    sa_file = str(auth.get("service_account_file") or "").strip()
    if not sa_file:
        _log.warning("drive_comments_auth: needs a service_account_file; channel off")
        return None
    if not Path(sa_file).exists():
        _log.warning("drive_comments_auth: %s does not exist; channel off", sa_file)
        return None
    scopes = [str(x) for x in (auth.get("scopes") or COMMENT_WRITE_SCOPES)]
    subject = str(auth.get("subject") or "").strip() or None
    try:
        return DriveComments(token_provider=service_account_token_provider(
            service_account_file=sa_file, subject=subject, scopes=scopes))
    except Exception as e:  # noqa: BLE001
        _log.warning("drive_comments_auth: could not build the client (%s); channel off", e)
        return None


def _build_inbound_mail(auth: Dict[str, Any]) -> Any:
    """An ``InboundMail`` client from ``{service_account_file, subject, allowed_addresses,
    scopes}``, or None.

    Read-only scope by default (``INBOUND_MAIL_READ_SCOPES``): this channel only ever needs to SEE
    what arrived, never to act on the mailbox, so a leaked key is bounded to reading, not sending
    or deleting. ``allowed_addresses`` defaults to just ``[subject]`` when the deployment names a
    subject but no explicit list -- domain-wide delegation can impersonate ANY mailbox in the
    domain, so an unconfigured allowlist means "read nothing" (fail closed), never "read whatever a
    card's spec happens to name."
    """
    try:
        from .adapters.inbound_mail import INBOUND_MAIL_READ_SCOPES, InboundMail
    except Exception as e:  # noqa: BLE001 -- an optional channel never blocks a lane starting
        _log.warning("inbound_mail_auth: channel unavailable (%s)", e)
        return None

    sa_file = str(auth.get("service_account_file") or "").strip()
    if not sa_file:
        _log.warning("inbound_mail_auth: needs a service_account_file; channel off")
        return None
    if not Path(sa_file).exists():
        _log.warning("inbound_mail_auth: %s does not exist; channel off", sa_file)
        return None
    scopes = [str(x) for x in (auth.get("scopes") or INBOUND_MAIL_READ_SCOPES)]
    subject = str(auth.get("subject") or "").strip() or None
    allowed = [str(a).strip().lower() for a in (auth.get("allowed_addresses") or [])
              if str(a).strip()]
    if not allowed and subject:
        allowed = [subject.lower()]
    if not allowed:
        _log.warning(
            "inbound_mail_auth: no allowed_addresses (and no subject to default from); channel "
            "off -- domain-wide delegation can impersonate any mailbox, so a deployment must name "
            "which ones this credential may actually read")
        return None
    try:
        return InboundMail(
            service_account_file=sa_file, scopes=scopes, allowed_addresses=allowed)
    except Exception as e:  # noqa: BLE001
        _log.warning("inbound_mail_auth: could not build the client (%s); channel off", e)
        return None


def _build_notion(block: Dict[str, Any]) -> Any:
    """A read-only ``NotionAdapter`` from ``{token_env | token_file, database_ids, ...}``, or None.

    Built only when a token is actually available (the named environment variable is set and
    non-empty, or the named file holds one) and at least one database id is given. The token is
    read lazily on every request (so a rotated one is picked up) and never logged.
    """
    try:
        from .adapters.notion_adapter import (
            NotionAdapter, env_token_provider, file_token_provider, normalize_id)
    except Exception as e:  # noqa: BLE001 -- an optional channel never blocks a lane starting
        _log.warning("notion: channel unavailable (%s)", e)
        return None

    databases = block.get("database_ids")
    clean = {str(alias).strip(): str(ident).strip() for alias, ident in (databases or {}).items()
             if str(alias).strip() and normalize_id(ident)} if isinstance(databases, dict) else {}
    if not clean:
        _log.warning("notion: no database_ids (alias -> database id); channel off. Nothing is read "
                     "unless a database is named")
        return None
    token_env = str(block.get("token_env") or "").strip()
    token_file = str(block.get("token_file") or "").strip()
    providers = []
    if token_env:
        providers.append(env_token_provider(token_env))
    if token_file:
        providers.append(file_token_provider(token_file))
    if not providers:
        _log.warning("notion: needs token_env (the NAME of an environment variable) or token_file; "
                     "channel off")
        return None

    def token() -> Optional[str]:
        for provider in providers:
            value = provider()
            if value:
                return value
        return None

    if not token():
        _log.warning("notion: no token available from %s; channel off",
                     " or ".join(x for x in (f"env {token_env}" if token_env else "",
                                             f"file {token_file}" if token_file else "") if x))
        return None
    try:
        kwargs: Dict[str, Any] = {}
        if block.get("max_rows_per_database"):
            kwargs["max_rows_per_database"] = int(block["max_rows_per_database"])
        if block.get("cache_ttl_seconds") is not None:
            kwargs["cache_ttl_seconds"] = float(block["cache_ttl_seconds"])
        adapter = NotionAdapter(token_provider=token, database_ids=clean, **kwargs)
    except Exception as e:  # noqa: BLE001
        _log.warning("notion: could not build the adapter (%s); channel off", e)
        return None
    _log.info("Notion wired (read-only, databases: %s)", sorted(clean))
    return adapter


def _build_google_chat(block: Dict[str, Any]) -> Any:
    """A read-only ``GoogleChatAdapter`` from ``{service_account_file, subject, space_names, ...}``.

    FAILS CLOSED on the allowlist: with no ``space_names`` nothing is wired and the log says why,
    since domain-wide delegation can otherwise read every space the subject is in. Scopes default
    to the read-only pair. Built only when the key file exists.
    """
    try:
        from .adapters.google_chat_adapter import (
            DEFAULT_CHAT_SCOPES, GoogleChatAdapter, service_account_token_provider)
    except Exception as e:  # noqa: BLE001 -- an optional channel never blocks a lane starting
        _log.warning("google_chat: channel unavailable (%s)", e)
        return None

    spaces = [str(x).strip() for x in (block.get("space_names") or []) if str(x).strip()]
    raw_all = block.get("all_spaces", False)
    if isinstance(raw_all, str):
        raw_all = raw_all.strip().lower() in ("1", "true", "yes", "on")
    all_spaces = bool(raw_all)
    if not spaces and not all_spaces:
        _log.warning("google_chat: no space_names; channel off -- domain-wide delegation can read "
                     "every space the subject is in, so a deployment must name which ones this "
                     "credential may read (or opt in to all of them with all_spaces = true)")
        return None
    sa_file = str(block.get("service_account_file") or "").strip()
    if not sa_file:
        _log.warning("google_chat: needs a service_account_file; channel off")
        return None
    if not Path(sa_file).exists():
        _log.warning("google_chat: %s does not exist; channel off", sa_file)
        return None
    scopes = [str(x) for x in (block.get("scopes") or DEFAULT_CHAT_SCOPES)]
    subject = str(block.get("subject") or "").strip() or None
    try:
        lookback = block.get("lookback_days")
        adapter = GoogleChatAdapter(
            token_provider=service_account_token_provider(
                service_account_file=sa_file, subject=subject, scopes=scopes),
            space_names=spaces,
            all_spaces=all_spaces,
            lookback_days=30 if lookback is None else int(lookback),
            assistant_senders=[str(x) for x in (block.get("assistant_senders") or [])],
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("google_chat: could not build the adapter (%s); channel off", e)
        return None
    if all_spaces:
        _log.info("Google Chat wired (read-only, ALL spaces the subject belongs to)")
    else:
        _log.info("Google Chat wired (read-only, %d allowlisted space(s))", len(spaces))
    return adapter
