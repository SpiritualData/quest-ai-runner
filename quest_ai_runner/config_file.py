"""Parse a TOML file into a ``RunnerConfig``: the loader behind ``RunnerConfig.from_file``.

Split out of ``config.py``. The set of file-expressible fields stays next to the dataclass in
``config.py`` (it must change with the fields), and is passed in; this module owns the parsing and
the loud failure on anything a file cannot express.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any, Dict, Set, Type, Union

from .config_errors import ConfigFileError


def load_runner_config_file(cls: Type[Any], path: Union[str, "os.PathLike[str]"], *,
                            scalar_fields: Set[str], special_fields: Set[str]) -> Any:
    """Build ``cls`` (``RunnerConfig``) from the TOML file at ``path``; see ``RunnerConfig.from_file``."""
    # expanduser so a documented "~/..." path works from a script, a systemd unit and a shell
    # alike, instead of only from whichever of them happened to expand it first.
    p = Path(path).expanduser()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigFileError(f"could not read config file {p}: {e}") from e
    try:
        import tomllib
    except ModuleNotFoundError as e:  # pragma: no cover - this repo's target is 3.12
        raise ConfigFileError(
            "RunnerConfig.from_file() needs the stdlib 'tomllib' module (Python 3.11+); this "
            "interpreter is older. Upgrade Python, or parse the file yourself and construct "
            "RunnerConfig(...) directly."
        ) from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigFileError(f"config file {p} is not valid TOML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigFileError(f"config file {p} must be a table at the top level")

    data = dict(data)
    personas_table = data.pop("personas", None)
    servers_tables = data.pop("mcp_servers", None)

    all_field_names = {f.name for f in dataclasses.fields(cls)}
    not_file_expressible = all_field_names - scalar_fields - special_fields
    unknown = set(data) - all_field_names
    unsupported = set(data) & not_file_expressible
    if unknown:
        raise ConfigFileError(
            f"config file {p} has key(s) that are not RunnerConfig fields at all: "
            f"{', '.join(sorted(unknown))}. Refusing to load rather than silently ignoring "
            f"them — a typo here would otherwise drop the setting instead of applying it."
        )
    if unsupported:
        raise ConfigFileError(
            f"config file {p} sets key(s) RunnerConfig cannot take from a file: "
            f"{', '.join(sorted(unsupported))} (each holds a live adapter, callable, or "
            f"object, not plain data). Wire it in Python, or via its matching QAR_* "
            f"environment variable, instead — see docs/writing-a-consumer.md."
        )

    kwargs: Dict[str, Any] = dict(data)
    if servers_tables is not None:
        kwargs["mcp_servers"] = mcp_server_specs(servers_tables, p)
    if personas_table is not None:
        if not isinstance(personas_table, dict):
            raise ConfigFileError(f"config file {p}: [personas] must be a table")
        from .runner.personas import PersonaResolverConfig

        persona_fields = {f.name for f in dataclasses.fields(PersonaResolverConfig)}
        unknown_persona = set(personas_table) - persona_fields
        if unknown_persona:
            raise ConfigFileError(
                f"config file {p}: [personas] has key(s) that are not PersonaResolverConfig "
                f"fields: {', '.join(sorted(unknown_persona))}"
            )
        try:
            kwargs["personas"] = PersonaResolverConfig(**personas_table)
        except TypeError as e:
            raise ConfigFileError(f"config file {p}: [personas] table is invalid ({e})") from e

    try:
        return cls(**kwargs)
    except TypeError as e:
        raise ConfigFileError(f"config file {p} could not build a RunnerConfig: {e}") from e


def mcp_server_specs(tables: Any, path: Any) -> list:
    """``[[mcp_servers]]`` -> ``MCPServerSpec`` list. A table holds the spec's plain-data fields; ``token_env`` names an
    environment variable that holds the bearer token for an http server (the token itself never sits in the file)."""
    from .adapters.mcp_client import MCPServerSpec

    if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
        raise ConfigFileError(f"config file {path}: mcp_servers must be an array of tables ([[mcp_servers]])")
    allowed = {f.name for f in dataclasses.fields(MCPServerSpec)} - {"token_provider"} | {"token_env"}
    specs = []
    for table in tables:
        unknown = set(table) - allowed
        if unknown:
            raise ConfigFileError(f"config file {path}: [[mcp_servers]] has key(s) that are not MCPServerSpec fields: "
                                  f"{', '.join(sorted(unknown))}")
        fields = {k: v for k, v in table.items() if k != "token_env"}
        if table.get("token_env"):
            env_name = str(table["token_env"])
            fields["token_provider"] = lambda name=env_name: os.environ.get(name) or None
        try:
            specs.append(MCPServerSpec(**fields))
        except TypeError as e:
            raise ConfigFileError(f"config file {path}: [[mcp_servers]] entry is invalid ({e})") from e
    return specs
