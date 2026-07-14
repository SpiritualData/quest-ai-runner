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
# resolvers; the data-backed types are consumer-injected (and degrade to an unresolved-pointer line
# when absent). ``conversation`` resolves via a local Claude session file by conv_id
# (``ClaudeConversationsAdapter.resolve_reference``); ``chat_thread`` resolves a Google Chat thread
# by re-fetching it through ``GoogleChatAdapter.resolve_reference``. Both are just RetrievalAdapters
# advertising a ``reference_type`` -- their own ``resolve_reference`` wires straight in as the
# resolver (see ``build_resolver_registry``). Kept here so this module and the store agree on the
# vocabulary.
CONTENT_TYPES = ("file", "collection", "conversation", "chat_thread", "query", "note")


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
        if item_type == "chat_thread":
            tid = str(loc.get("thread_or_message_id") or loc.get("id") or "?").strip() or "?"
            space = str(loc.get("space") or "").strip()
            return f"[chat_thread ref: {tid}{(' in ' + space) if space else ''} (unresolved)]"
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


def coerce_resolver(resolver: Any) -> Optional[Any]:
    """Normalize a registry value into a ReferenceResolver (an object with ``resolve``).

    Accepts either a ReferenceResolver object (has a callable ``resolve``) -- returned unchanged --
    or a BARE CALLABLE with the ``resolve``/``resolve_reference`` signature ``fn(locator, *,
    max_chars) -> Optional[str]`` (e.g. a resolvable adapter's ``resolve_reference`` bound method),
    which is wrapped so ``.resolve`` calls it. This is what lets an adapter's ``resolve_reference`` be
    wired DIRECTLY into ``consumer_resolvers`` with no wrapper class. Returns ``None`` for anything
    that is neither. Never raises.
    """
    try:
        if resolver is None:
            return None
        if callable(getattr(resolver, "resolve", None)):
            return resolver

        if callable(resolver):
            fn = resolver

            class _CallableResolver:
                def resolve(self, locator: Dict[str, Any], *, max_chars: int = 2000) -> str:
                    try:
                        return fn(locator, max_chars=max_chars) or ""
                    except Exception:  # noqa: BLE001 — a resolver must never raise
                        return ""

            return _CallableResolver()
    except Exception:  # noqa: BLE001
        return None
    return None


def collect_reference_resolvers(retrieval: Optional[Any]) -> Dict[str, Any]:
    """Discover ``{reference_type: resolve_reference}`` from any resolvable retrieval adapter.

    Walks a retrieval adapter (or a composite exposing an ``adapters`` list, recursively) and, for
    each one that advertises the RetrievalAdapter reference-resolution capability -- a non-None
    ``reference_type`` and a callable ``resolve_reference`` (see ``core.adapters.RetrievalAdapter``)
    -- maps its type to its OWN ``resolve_reference`` bound method. That method mirrors
    ``ReferenceResolver.resolve``'s signature/contract exactly, so it can be dropped straight into
    ``build_resolver_registry``'s ``consumer_resolvers`` with no wrapper. Fully DUCK-TYPED (never
    imports the adapter classes), so a consumer's own resolvable adapter participates automatically.
    Earlier adapters win a type collision. Never raises -- returns ``{}`` on any problem.
    """
    out: Dict[str, Any] = {}

    def visit(node: Any) -> None:
        if node is None:
            return
        children = getattr(node, "adapters", None)
        if isinstance(children, (list, tuple)):
            for child in children:
                visit(child)
        ref_type = getattr(node, "reference_type", None)
        resolve = getattr(node, "resolve_reference", None)
        if isinstance(ref_type, str) and ref_type and callable(resolve) and ref_type not in out:
            out[ref_type] = resolve

    try:
        visit(retrieval)
    except Exception:  # noqa: BLE001 — discovery must never break wiring
        return out
    return out


def build_resolver_registry(
    *,
    file_read_text: Optional[Callable[[str, int], str]] = None,
    consumer_resolvers: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the ``{type: ReferenceResolver}`` registry: built-ins + consumer-injected.

    Built-ins:
      * ``note``  — always wired (``NoteResolver``).
      * ``file``  — wired only when ``file_read_text`` is supplied (the store's fresh-read fn).

    ``consumer_resolvers`` (e.g. from ``RunnerConfig.reference_resolvers``) supplies the data-backed
    types (``collection`` / ``conversation`` / ``chat_thread`` / ``query``) or overrides a built-in.
    Each value may be a ReferenceResolver object OR a bare callable with the ``resolve`` signature
    (e.g. a resolvable adapter's ``resolve_reference`` bound method) -- bare callables are coerced via
    ``coerce_resolver``. Consumer entries WIN on key collision, so a host can replace the built-in
    ``file`` resolver if it wants. Any type absent from the final registry degrades to an
    unresolved-pointer line at render time. Never raises.
    """
    registry: Dict[str, Any] = {"note": NoteResolver()}
    if file_read_text is not None:
        registry["file"] = make_file_resolver(file_read_text)
    for key, resolver in (consumer_resolvers or {}).items():
        if isinstance(key, str) and key:
            coerced = coerce_resolver(resolver)
            if coerced is not None:
                registry[key] = coerced
    return registry
