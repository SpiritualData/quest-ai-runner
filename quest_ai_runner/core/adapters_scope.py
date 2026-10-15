"""Usage scope plumbing: the thread-local scope, scoped executors and counters.

Split out of ``adapters.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import contextlib
import threading
import weakref
from typing import Any, Callable, Dict, Optional
from concurrent.futures import ThreadPoolExecutor


# ---------------------------------------------------------------------------
# PER-TURN USAGE ACCOUNTING (token counters that are shared safely)
# ---------------------------------------------------------------------------
#
# A ``ModelProvider`` instance is not one-run-at-a-time: quest-ai-runner's ``Poller`` builds ONE
# ``Orchestrator`` (and hence one provider, see ``build_orchestrator``) and reuses it for every
# task, and ``max_concurrent_tasks`` (default 2) runs several tasks' ``Orchestrator.run()`` calls
# CONCURRENTLY on a thread pool. ``Orchestrator.run()`` resets a provider's ``tokens_in``/
# ``tokens_out`` to 0 at the start of "the current turn" and reads them at the end to report what
# THIS turn cost, which is only true if nothing else writes the same counters meanwhile. A plain
# ``int`` instance attribute breaks that: one task's reset can zero a sibling's in-flight count,
# and both tasks' calls add into the SAME total.
#
# So each counter value lives in a USAGE SCOPE, not on the instance. A scope defaults to the
# calling thread, and a worker thread that a turn spawns INHERITS its parent's scope (see
# ``ScopedThreadPoolExecutor``) so parallel sub-answers, overseer consults and guidance calls
# still land in the turn that paid for them instead of vanishing into a per-thread bucket nobody
# reads. No call site (``self.tokens_in = 0``, ``self.tokens_in += n``,
# ``getattr(obj, "tokens_in", 0)``) changes.

_usage_scope_state = threading.local()


def current_usage_scope() -> Any:
    """The scope key whose counters the calling thread reads and writes.

    Defaults to the thread itself, so an unbound thread keeps its own isolated tally.
    """
    scope = getattr(_usage_scope_state, "scope", None)
    if scope is None:
        scope = threading.current_thread()
    return scope


@contextlib.contextmanager
def bound_usage_scope(scope: Any):
    """Run the block attributing provider usage to ``scope`` instead of this thread's own."""
    previous = getattr(_usage_scope_state, "scope", None)
    _usage_scope_state.scope = scope
    try:
        yield
    finally:
        _usage_scope_state.scope = previous


class ScopedThreadPoolExecutor(ThreadPoolExecutor):
    """A ThreadPoolExecutor whose workers bill provider usage to the CREATING thread's scope.

    The orchestrator spawns these inside a turn (parallel subquestion answers, the overseer
    consult, context/guidance calls). Those are the turn's own model calls, so their tokens must
    show up in the turn's reported total; with a plain pool they would be counted against each
    anonymous worker thread and silently dropped from the number the user sees.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.usage_scope = current_usage_scope()

    def submit(self, fn: Callable, /, *args: Any, **kwargs: Any):  # type: ignore[override]
        scope = self.usage_scope

        def scoped(*inner_args: Any, **inner_kwargs: Any):
            with bound_usage_scope(scope):
                return fn(*inner_args, **inner_kwargs)

        return super().submit(scoped, *args, **kwargs)


class ScopedCounter:
    """A per-usage-scope numeric counter that reads and writes like a plain instance attribute.

    Values are stored per (scope, attribute) on the owning object, keyed weakly so a finished
    thread's tally is collected with the thread. See the module note above for why the counters
    cannot simply live on the instance.
    """

    def __init__(self, default: float = 0):
        self._default = default

    def __set_name__(self, owner: type, name: str) -> None:
        self._name = name

    def _values(self, obj: Any) -> Dict[str, Any]:
        store = obj.__dict__.get("usage_scope_counters")
        if store is None:
            store = weakref.WeakKeyDictionary()
            obj.__dict__["usage_scope_counters"] = store
        scope = current_usage_scope()
        try:
            values = store.get(scope)
        except TypeError:  # unhashable / non-weakref-able scope: fall back to a plain tally
            return obj.__dict__.setdefault("usage_fallback_counters", {})
        if values is None:
            values = {}
            store[scope] = values
        return values

    def __get__(self, obj: Any, objtype: Optional[type] = None):
        if obj is None:
            return self
        return self._values(obj).get(self._name, self._default)

    def __set__(self, obj: Any, value: Any) -> None:
        self._values(obj)[self._name] = value


# Legacy name kept so existing imports keep working; the counters are scope-based now.
ThreadLocalCounter = ScopedCounter
