"""Reference resolvers — turn a card's typed content item into fresh rendered text.

A context card (see ``file_context_store``) is a persistent, source-agnostic topic card. Its
content is a list of typed ITEMS, each either a REFERENCE (a pointer resolved fresh to current
content every time the card is used) or an LLM NOTE (synthesized text that resolves to itself).
This module defines the resolution boundary:

  * ``ReferenceResolver`` — a tiny Protocol: ``resolve(locator, *, max_chars) -> str``. It NEVER
    raises; on any failure it returns ``""`` so a bad pointer can never break assembly. One
    resolver handles one item ``type`` ("file" | "collection" | "conversation" | "query" | "note").

  * The BUILT-IN resolvers — ``note`` (returns the locator text) and an optional ``file`` resolver
    factory (reuses the store's own fresh file-read path). These ship with the library because
    they need no consumer-specific data access.

  * ``collection`` / ``conversation`` / ``query`` resolvers are INJECTED by the consumer (they hit
    the consumer's data layer, which the library must stay ignorant of — see hard rule #2). When a
    type has no resolver wired, resolution renders a short graceful UNRESOLVED-POINTER line instead
    of failing, so the card still surfaces what it points at.

The registry is just a ``{type: ReferenceResolver}`` dict. ``build_resolver_registry`` merges the
built-ins with a consumer-supplied dict (consumer entries win). Generic by construction: no org,
collection, or path specifics live here.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

# Item ``type`` values understood by the card content model. ``note`` and ``file`` have built-in
# resolvers; the other three are consumer-injected (and degrade to an unresolved-pointer line when
# absent). Kept here so both this module and the store agree on the vocabulary.
CONTENT_TYPES = ("file", "collection", "conversation", "query", "note")


@runtime_checkable
class ReferenceResolver(Protocol):
    """Resolve ONE content item's locator into fresh rendered text. NEVER raises.

    ``locator`` is the item's type-specific pointer dict (e.g. ``{"path": ...}`` for a file,
    ``{"name", "id", "query"}`` for a collection, ``{"text": ...}`` for a note). ``max_chars`` is a
    soft budget the resolver SHOULD respect (truncate rather than overrun). Return the rendered text,
    or ``""`` when the item resolves to nothing or anything goes wrong — assembly treats ``""`` as
    "this item contributed nothing" and moves on, so a resolver must swallow its own errors.
    """

    def resolve(self, locator: Dict[str, Any], *, max_chars: int = 2000) -> str:
        """Return fresh rendered text for ``locator`` (or "" on empty/failure). Never raises."""


def _render_unresolved(item_type: str, locator: Dict[str, Any]) -> str:
    """Render a short, graceful placeholder for a reference whose type has no resolver wired.

    This is what keeps an un-wired ``collection`` / ``conversation`` / ``query`` reference from
    failing: instead of resolving the live content, the card still shows WHAT it points at, marked
    clearly as unresolved. Generic: it reads only the common locator keys, never anything org- or
    type-specific. Never raises.
    """
    try:
        loc = locator or {}
        if item_type == "collection":
            name = str(loc.get("name") or loc.get("collection") or "").strip()
            cid = str(loc.get("id") or "").strip()
            tail = "/".join(p for p in (name, cid) if p) or "?"
            q = str(loc.get("query") or "").strip()
            return f"[collection ref: {tail}{(' query=' + q) if q else ''} (unresolved)]"
        if item_type == "conversation":
            cid = str(loc.get("conv_id") or loc.get("id") or "?").strip() or "?"
            q = str(loc.get("query") or "").strip()
            return f"[conversation ref: {cid}{(' query=' + q) if q else ''} (unresolved)]"
        if item_type == "query":
            q = str(loc.get("query") or loc.get("text") or "?").strip() or "?"
            return f"[query ref: {q} (unresolved)]"
        # Unknown/other type: name whatever identifier we can find.
        ident = str(loc.get("name") or loc.get("id") or loc.get("path") or "?").strip() or "?"
        return f"[{item_type} ref: {ident} (unresolved)]"
    except Exception:  # noqa: BLE001 — a placeholder must never raise
        return f"[{item_type} ref (unresolved)]"


class NoteResolver:
    """Built-in resolver for ``note`` items: returns the locator's ``text`` verbatim (capped).

    A note is LLM-authored synthesized text held ON the card; it has no external source, so
    "resolving" it just returns the stored text. Never raises.
    """

    def resolve(self, locator: Dict[str, Any], *, max_chars: int = 2000) -> str:
        try:
            text = str((locator or {}).get("text") or "")
            if len(text) > max_chars:
                text = text[: max_chars - 1].rstrip() + "…"
            return text
        except Exception:  # noqa: BLE001
            return ""


def make_file_resolver(read_text: Callable[[str, int], str]) -> "ReferenceResolver":
    """Build the built-in ``file`` resolver from a ``read_text(path, max_chars) -> str`` callable.

    The library does NOT bake in a filesystem access policy — the store owns that (path
    resolution against its repo_root, size caps, staleness). So the store passes its own fresh
    file-read function here and this wrapper adapts it to the ``ReferenceResolver`` interface.
    The locator is ``{"path": "<rel-or-abs path>"}``. Returns "" on any failure; never raises.
    """

    class _FileResolver:
        def resolve(self, locator: Dict[str, Any], *, max_chars: int = 2000) -> str:
            try:
                path = str((locator or {}).get("path") or "").strip()
                if not path:
                    return ""
                return read_text(path, max_chars) or ""
            except Exception:  # noqa: BLE001 — a fresh read failure resolves to nothing
                return ""

    return _FileResolver()


def build_resolver_registry(
    *,
    file_read_text: Optional[Callable[[str, int], str]] = None,
    consumer_resolvers: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the ``{type: ReferenceResolver}`` registry: built-ins + consumer-injected.

    Built-ins:
      * ``note``  — always wired (``NoteResolver``).
      * ``file``  — wired only when ``file_read_text`` is supplied (the store's fresh-read fn).

    ``consumer_resolvers`` (e.g. from ``RunnerConfig.reference_resolvers``) supplies ``collection``
    / ``conversation`` / ``query`` (or overrides a built-in). Consumer entries WIN on key collision,
    so a host can replace the built-in ``file`` resolver if it wants. Any type absent from the final
    registry degrades to an unresolved-pointer line at render time. Never raises.
    """
    registry: Dict[str, Any] = {"note": NoteResolver()}
    if file_read_text is not None:
        registry["file"] = make_file_resolver(file_read_text)
    for key, resolver in (consumer_resolvers or {}).items():
        if resolver is not None and isinstance(key, str) and key:
            registry[key] = resolver
    return registry
