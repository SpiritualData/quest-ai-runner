"""RECIPES: operations the assistant has already worked out once, replayed without a context search.

THE PROBLEM
-----------
A request like "mark my meditation habit done" costs the same on the hundredth ask as on the
first: understand the message, search context, plan, pick a tool, fill its arguments, answer. The
slow part is almost never the operation; it is rediscovering HOW to do it.

THE IDEA
--------
A recipe is a tool call that already worked, remembered with the request that produced it:

    {tool: "log_habit", example_args: {...}, examples: ["mark my meditation habit done", ...]}

Two halves, both generic (nothing here knows an org or a product):

  * ``RecipeStore.match`` is a lexical lookup: how much of each recipe's skeleton (its example
    request minus the words that became argument values) the new request contains. It reads no model
    and no index, so it costs microseconds and can run before anything else in a turn. It returns
    candidates only above a similarity floor.
  * ``RecipeRunner.try_fast_path`` takes the best candidate and settles two things with ONE small
    fast-tier call: does THIS request ask for the same operation (a recipe never fires on a
    near-miss such as "show my meditation habit"), and what arguments does it carry. An exact
    repeat of a saved request needs no model call at all: the saved arguments are reused as they
    are. The tool then runs through the normal ``ToolRegistry.invoke`` (validation, timeout, never
    raises), and the receipt is the answer.

A new operation is LEARNED, not authored: when the planner's own ``tool`` action succeeds, the loop
calls ``RecipeStore.learn(request, tool, args)`` and the next matching request skips the slow
path. Nothing is learned from a failure.

INVARIANTS
----------
  * Off unless a store is wired (``Orchestrator(recipes=...)``). Off means byte-for-byte the old run.
  * Every failure degrades to the normal path (``try_fast_path`` returns None); a recipe can make a
    turn faster, never make it fail.
  * Recipes honor ``scope_tags`` (core/scope_tags.py): a recipe learned inside quest X, whose
    example arguments may name X's content, is not offered to a turn scoped to quest Y.
  * A tool that is unknown to the registry now (removed, renamed) is never replayed.
  * Judgment about WHETHER a request is the same operation is the model's structured verdict
    (``applies``), never a keyword net over the user's words. The lexical match only nominates.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .scope_tags import as_tag_list, scope_tags_allow

log = logging.getLogger(__name__)

# Floor for nominating a recipe: the share of its skeleton words (the operation's words, see
# ``skeleton_of``) found in the request. Nomination is cheap
# and the verdict call below is the real gate, so this only has to keep unrelated requests from
# paying for that call.
DEFAULT_MIN_SCORE = 0.6
MAX_EXAMPLES_PER_RECIPE = 6
MAX_RECIPES = 300

_STOP = frozenset("""
a an the my me i our of to for on in at by with and or is are be it this that these those please
can could would you your do does did now just some any all up out into about from as so then
""".split())
_TOKEN = re.compile(r"[a-z0-9]+")


def content_tokens(text: str) -> frozenset:
    """Lowercased content words of ``text``: stop words dropped, a trailing plural 's' folded."""
    out = set()
    for tok in _TOKEN.findall((text or "").lower()):
        if tok in _STOP:
            continue
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        out.add(tok)
    return frozenset(out)


def normalize_request(text: str) -> str:
    return " ".join(_TOKEN.findall((text or "").lower()))


def similarity(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _value_tokens(value: Any) -> frozenset:
    """Content words of every string/number inside a tool-argument value."""
    if isinstance(value, dict):
        return frozenset().union(*(_value_tokens(v) for v in value.values())) if value else frozenset()
    if isinstance(value, (list, tuple)):
        return frozenset().union(*(_value_tokens(v) for v in value)) if value else frozenset()
    if isinstance(value, bool) or value is None:
        return frozenset()
    return content_tokens(str(value))


def skeleton_of(request: str, args: Dict[str, Any]) -> frozenset:
    """The words of ``request`` that name the OPERATION: the request minus the words that became
    argument values. "add a note saying buy oat milk" with text="buy oat milk" is {add, note, saying}.
    A later request for the same operation carries those words whatever its payload is, which is
    why this beats comparing whole requests (a long free-text payload swamps the overlap)."""
    return content_tokens(request) - _value_tokens(args)


def containment(skeleton: frozenset, request_tokens: frozenset) -> float:
    """Share of the skeleton's words present in the request."""
    if not skeleton:
        return 0.0
    return len(skeleton & request_tokens) / len(skeleton)


@dataclass
class Recipe:
    id: str
    tool: str
    example_args: Dict[str, Any]
    examples: List[str]
    scope_tags: List[str] = field(default_factory=list)
    skeleton: List[str] = field(default_factory=list)
    uses: int = 0
    learned_at: float = 0.0
    last_used: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "tool": self.tool, "example_args": self.example_args,
                "examples": self.examples, "scope_tags": self.scope_tags, "skeleton": self.skeleton, "uses": self.uses,
                "learned_at": self.learned_at, "last_used": self.last_used}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Recipe":
        return cls(id=str(d["id"]), tool=str(d["tool"]),
                   example_args=dict(d.get("example_args") or {}),
                   examples=[str(e) for e in (d.get("examples") or [])],
                   scope_tags=as_tag_list(d.get("scope_tags")),
                   skeleton=[str(t) for t in (d.get("skeleton") or [])], uses=int(d.get("uses") or 0),
                   learned_at=float(d.get("learned_at") or 0.0),
                   last_used=float(d.get("last_used") or 0.0))


class RecipeStore:
    """One JSON file of recipes. Lock-guarded, atomically rewritten, safe to share in a process.

    ``path=None`` keeps the store in memory only (tests, ephemeral consumers).
    """

    def __init__(self, path: Optional[os.PathLike] = None, *, min_score: float = DEFAULT_MIN_SCORE):
        self.path = Path(path) if path else None
        self.min_score = min_score
        self._lock = threading.RLock()
        self._recipes: Dict[str, Recipe] = {}
        self._loaded_mtime: Optional[float] = None
        self._load()

    # --- persistence --------------------------------------------------------------------

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            mtime = self.path.stat().st_mtime
            if self._loaded_mtime == mtime:
                return
            data = json.loads(self.path.read_text() or "{}")
            self._recipes = {r["id"]: Recipe.from_dict(r) for r in data.get("recipes", [])}
            self._loaded_mtime = mtime
        except Exception as e:  # noqa: BLE001 -- a corrupt store is an empty store, never a crash
            log.warning("Recipe store unreadable (%s: %s); starting empty", type(e).__name__, e)

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".recipes-")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump({"recipes": [r.to_dict() for r in self._recipes.values()]}, fh,
                              indent=1, default=str)
                try:
                    from .file_modes import match_umask
                    with open(tmp, "rb") as fh2:
                        match_umask(fh2.fileno())
                except Exception:  # noqa: BLE001
                    pass
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._loaded_mtime = self.path.stat().st_mtime
        except Exception as e:  # noqa: BLE001
            log.warning("Recipe store write failed: %s: %s", type(e).__name__, e)

    # --- lookup -------------------------------------------------------------------------

    def match(self, request: str, *, scope_tags: Any = None, limit: int = 3,
              tools: Optional[Any] = None) -> List[Tuple[Recipe, float]]:
        """Candidate recipes for ``request``, best first, each with its similarity score.

        ``tools`` (a ToolRegistry, optional) drops recipes whose tool no longer exists.
        """
        req = content_tokens(request)
        if len(req) < 2:
            return []
        with self._lock:
            self._load()
            scored: List[Tuple[Recipe, float]] = []
            for r in self._recipes.values():
                if not scope_tags_allow(r.scope_tags, scope_tags):
                    continue
                if tools is not None and tools.get(r.tool) is None:
                    continue
                if len(r.skeleton) >= 2:
                    best = containment(frozenset(r.skeleton), req)
                else:       # a recipe saved without a usable skeleton: compare whole requests
                    best = max((similarity(req, content_tokens(e)) for e in r.examples), default=0.0)
                if best >= self.min_score:
                    scored.append((r, best))
        scored.sort(key=lambda p: (-p[1], -p[0].uses))
        return scored[:limit]

    def exact(self, recipe: Recipe, request: str) -> bool:
        norm = normalize_request(request)
        return any(normalize_request(e) == norm for e in recipe.examples)

    # --- learning -----------------------------------------------------------------------

    def learn(self, request: str, tool: str, args: Dict[str, Any], *,
              scope_tags: Any = None) -> Optional[Recipe]:
        """Remember that ``tool`` with ``args`` did what ``request`` asked. Never raises."""
        try:
            request = (request or "").strip()
            if not request or not tool or len(request) > 600 or len(content_tokens(request)) < 2:
                return None
            tags = as_tag_list(scope_tags)
            now = time.time()
            with self._lock:
                self._load()
                for r in self._recipes.values():
                    if r.tool != tool or r.scope_tags != tags:
                        continue
                    if r.example_args == dict(args or {}) or any(
                            similarity(content_tokens(request), content_tokens(e)) >= 0.8
                            for e in r.examples):
                        if not any(normalize_request(e) == normalize_request(request)
                                   for e in r.examples):
                            r.examples = ([request] + r.examples)[:MAX_EXAMPLES_PER_RECIPE]
                        r.example_args = dict(args or {})
                        sk = skeleton_of(request, args or {})
                        common = frozenset(r.skeleton) & sk
                        if len(common) >= 2:
                            r.skeleton = sorted(common)
                        self._save()
                        return r
                recipe = Recipe(id=f"recipe-{len(self._recipes) + 1}-{int(now)}", tool=tool,
                                example_args=dict(args or {}), examples=[request],
                                scope_tags=tags, skeleton=sorted(skeleton_of(request, args or {})),
                                learned_at=now)
                self._recipes[recipe.id] = recipe
                if len(self._recipes) > MAX_RECIPES:
                    # Forget the least recently useful, not the newest.
                    victim = min(self._recipes.values(),
                                 key=lambda r: (r.uses, r.last_used or r.learned_at))
                    self._recipes.pop(victim.id, None)
                self._save()
                return recipe
        except Exception as e:  # noqa: BLE001
            log.warning("Recipe learn failed: %s: %s", type(e).__name__, e)
            return None

    def mark_used(self, recipe: Recipe, request: str) -> None:
        with self._lock:
            recipe.uses += 1
            recipe.last_used = time.time()
            if not any(normalize_request(e) == normalize_request(request) for e in recipe.examples):
                recipe.examples = ([request] + recipe.examples)[:MAX_EXAMPLES_PER_RECIPE]
            self._save()

    def forget(self, recipe_id: str) -> None:
        with self._lock:
            if self._recipes.pop(recipe_id, None) is not None:
                self._save()

    def all(self) -> List[Recipe]:
        with self._lock:
            self._load()
            return list(self._recipes.values())


RECIPE_ARGS_TOOL: Dict[str, Any] = {
    "name": "recipe_args",
    "description": "Decide whether the request repeats a known operation, and fill its arguments.",
    "input_schema": {
        "type": "object",
        "properties": {
            "applies": {"type": "boolean",
                        "description": "True only when the NEW request asks for the same kind of "
                                       "operation as the saved example, performed now."},
            "args": {"type": "object",
                     "description": "The tool arguments for the NEW request, in the same shape as "
                                    "the saved example arguments. Empty when applies is false."},
        },
        "required": ["applies"],
    },
}

RECIPE_ARGS_PROMPT = """\
A saved recipe performs an operation with a tool. Decide whether the NEW request asks for that same
operation, and if so write the tool's arguments for it. Answer nothing else.

TOOL: {tool}
WHAT IT DOES: {description}
ARGUMENT SCHEMA: {schema}

SAVED EXAMPLE
  request: {example_request}
  arguments: {example_args}

NEW REQUEST: {request}

Rules:
  * applies = true only when the NEW request wants the same kind of operation done now. A question
    ABOUT the thing ("show", "what is", "how many"), a different operation on it, or a request that
    needs more than this one tool call is applies = false.
  * Take values from the NEW request, never from the example. Keep example values only for
    arguments the NEW request does not change and that are not about its content.
  * A flag that asserts the user explicitly asked for something (for example
    user_asked_for_this_field) is true only if the NEW request itself explicitly asks for it.
  * If a required argument cannot be taken from the NEW request, applies = false.
"""


@dataclass
class RecipeOutcome:
    """What a fast-path attempt produced. ``text`` is the receipt to answer with."""
    recipe_id: str
    tool: str
    args: Dict[str, Any]
    text: str
    ok: bool
    mutates: bool
    llm_calls: int
    seconds: float


class RecipeRunner:
    """Glue between a store, a tool registry and a provider. Stateless apart from the store."""

    def __init__(self, store: RecipeStore, tools: Any, *, fill_args):
        """``fill_args(prompt) -> dict`` makes the one small model call (``{"applies", "args"}``)."""
        self.store = store
        self.tools = tools
        self._fill_args = fill_args

    def nominate(self, request: str, scope_tags: Any = None) -> List[Tuple[Recipe, float]]:
        return self.store.match(request, scope_tags=scope_tags, tools=self.tools)

    def try_fast_path(self, request: str, ctx: Any, *, scope_tags: Any = None,
                      candidates: Optional[List[Tuple[Recipe, float]]] = None,
                      allow_mutating: bool = True) -> Optional[RecipeOutcome]:
        """Run the best matching recipe for ``request``, or return None to take the normal path.

        Never raises. A tool that FAILS is reported as None too (the normal path then retries it
        with full context), and the recipe is forgotten after it fails on an exact repeat.
        """
        started = time.monotonic()
        try:
            candidates = candidates if candidates is not None else self.nominate(request, scope_tags)
            if not candidates:
                return None
            recipe, _score = candidates[0]
            spec = self.tools.get(recipe.tool)
            if spec is None or (spec.mutates and not allow_mutating):
                return None
            llm_calls = 0
            if not spec.mutates and self.store.exact(recipe, request):
                # Zero model calls, read-only tools only: a mutating repeat may lean on the
                # conversation ("add that"), so it always has its arguments re-derived.
                args = dict(recipe.example_args)
            else:
                prompt = RECIPE_ARGS_PROMPT.format(
                    tool=spec.name, description=(spec.description or "")[:400],
                    schema=json.dumps(spec.parameters or {}, separators=(",", ":"))[:1500],
                    example_request=recipe.examples[0],
                    example_args=json.dumps(recipe.example_args, default=str)[:800],
                    request=request)
                verdict = self._fill_args(prompt)
                llm_calls = 1
                if not isinstance(verdict, dict) or verdict.get("applies") is not True:
                    return None
                args = verdict.get("args")
                if not isinstance(args, dict) or not args:
                    return None
            result = self.tools.invoke(recipe.tool, args, ctx)
            if not result.ok:
                log.info("Recipe %s failed (%s); taking the normal path", recipe.id, result.text[:160])
                return None
            self.store.mark_used(recipe, request)
            return RecipeOutcome(recipe_id=recipe.id, tool=recipe.tool, args=args,
                                 text=result.text, ok=True, mutates=bool(spec.mutates),
                                 llm_calls=llm_calls, seconds=time.monotonic() - started)
        except Exception as e:  # noqa: BLE001 -- a recipe must never break a turn
            log.warning("Recipe fast path failed, taking the normal path: %s: %s",
                        type(e).__name__, e)
            return None


def recipes_enabled_from_env(env: Optional[Dict[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("QAR_RECIPES") or "").strip().lower() in ("1", "true", "on", "yes")


def build_recipe_store_from_env(env: Optional[Dict[str, str]] = None) -> Optional[RecipeStore]:
    """A file-backed store under ``QAR_RECIPES_DIR`` (default ``<QAR_CORPUS_ROOT>/.quest-context``)
    when ``QAR_RECIPES`` is on, else None."""
    env = os.environ if env is None else env
    if not recipes_enabled_from_env(env):
        return None
    base = env.get("QAR_RECIPES_DIR") or os.path.join(
        env.get("QAR_CORPUS_ROOT") or ".", ".quest-context", "recipes")
    min_score = float(env.get("QAR_RECIPES_MIN_SCORE") or DEFAULT_MIN_SCORE)
    return RecipeStore(Path(base) / "recipes.json", min_score=min_score)
