"""The runner's view of the config layer's builders, injected rather than imported.

The config layer sits above the runner (``core < adapters < runner < config``), yet the poller and
the channel runner must build a brain, derive capabilities, and resolve the tool catalog and the
rep-sync resolver from a ``RunnerConfig``. Importing ``quest_ai_runner.config`` from here would point
outward and close an import cycle, so those four builders are registered once by the package
``__init__`` (the same pattern core uses for the model translator and the Quest client factory) and
looked up through this module.

Each registered builder resolves the config module's current function when called, so a test that
patches ``quest_ai_runner.config.build_orchestrator`` still takes effect.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

_builders: Dict[str, Callable[..., Any]] = {}


def install_config_builders(**builders: Callable[..., Any]) -> None:
    """Register ``build_orchestrator``, ``derive_capabilities``, ``resolve_rep_sync_resolver`` and
    ``resolve_tool_registry`` (any subset; a later call replaces the earlier one by name)."""
    _builders.update(builders)


def _need(name: str) -> Callable[..., Any]:
    fn: Optional[Callable[..., Any]] = _builders.get(name)
    if fn is None:
        raise RuntimeError(
            f"the config layer has not registered {name}; import quest_ai_runner (the package root "
            "wires it) before using the runner")
    return fn


def build_orchestrator(cfg: Any, **kwargs: Any) -> Any:
    return _need("build_orchestrator")(cfg, **kwargs)


def derive_capabilities(cfg: Any) -> Any:
    return _need("derive_capabilities")(cfg)


def resolve_rep_sync_resolver(cfg: Any, **kwargs: Any) -> Any:
    return _need("resolve_rep_sync_resolver")(cfg, **kwargs)


def resolve_tool_registry(cfg: Any) -> Any:
    return _need("resolve_tool_registry")(cfg)
