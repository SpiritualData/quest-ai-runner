"""personas — decide WHICH persona runs a queued task, from configuration instead of code.

``RunnerConfig.rep_sync_resolver`` is a bare callable seam: ``task -> (id, skill_dir) | None``.
Every consumer that wants its tasks to run *as somebody* has had to hand-write that callable, and
in practice they all wrote the same machinery — a registry mapping ids to skill folders under a
skills root, plus a policy for picking one from a task. Two independent lanes grew their own copy
before this module existed. What differed between them was only POLICY:

  * a **structural** lane: the task already names its owner in a field (``assignee_user_id`` /
    ``user_id``), so resolution is a lookup with no model call at all, and an id nobody has a skill
    folder for is either auto-registered as a real skill or parked in a per-id cache dir;
  * a **character** lane: the task is prose written by a human, so a persona activates only when it
    is *asked for* — a structured assignment field, or an LLM judging the request as an explicit
    ask, or that persona's domain cards clearly dominating the subject matter. A bare mention of a
    persona's name must NEVER activate it (naming somebody is not asking them).

Both are expressible here as a :class:`PersonaResolverConfig`, and
:func:`build_persona_resolver` returns the callable to hand straight to
``RunnerConfig.rep_sync_resolver``::

    cfg = PersonaResolverConfig(skills_root=..., registry_file=..., cache_dir=..., auto_register=True)
    runner_cfg.rep_sync_resolver = build_persona_resolver(cfg, quest_client=client, team_id=team)

Precedence, first hit wins (a step whose flag is off costs nothing — it is never entered, so no
provider call and no card read happens for a lane that did not ask for it):

  1. **structured assignment** — a task field from ``assignment_fields`` whose value matches the
     registry exactly. No model call.
  2. **explicit ask, LLM-judged** (``llm_explicit_ask`` + a ``provider``) — one cheap structured
     call decides whether the requester is explicitly asking a persona to do this work.
  3. **domain-card dominance** (``card_activation`` + ``cards_dir``) — content-based activation
     from the personas' own domain cards, with each persona's own name/aliases excluded from
     scoring so a bare mention can never carry it.
  4. **structural fallback** — an id in one of the ``assignment_fields`` that the registry does not
     know: auto-register it as a real skill (``auto_register``), else park it under
     ``cache_dir/<id>``, else give up.

Nothing here raises: any failure is logged and yields ``None``, so a persona problem can never stop
a task from running as the plain assistant.

Registry files come in two real-world shapes and :meth:`PersonaRegistry.from_file` normalizes both,
deciding by the VALUE type:

  * rich (``{"<name>": {"rep_id": ..., "skill": ..., "display_name": ..., "aliases": [...]}}``) —
    the key is the persona's name, the id comes from ``rep_id``, the skill folder from ``skill``;
  * flat (``{"<id>": "<slug>"}``) — the key IS the id and the value IS the skill folder name.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from ..core.card_filter import _extract_json

log = logging.getLogger("quest-ai-runner.personas")

# Structured task fields that may carry an assignment. Matched EXACTLY against the registry (or,
# in the structural fallback, read as a bare id) — never scanned as prose. The default is the union
# of the field names real lanes use, most specific first, so ``assignee_user_id`` beats a plain
# ``user_id`` (the task owner) the way a structural lane needs. ``assignee_rep_id`` is the field a
# quest autopilot pass stamps the persona it resolved into; dropping it would silently discard an
# explicit routing decision, which is worse than having none.
DEFAULT_ASSIGNMENT_FIELDS: Tuple[str, ...] = (
    "assignee_rep_id", "assignee_user_id", "assignee", "assigned_to",
    "rep_id", "user_id", "persona", "character", "handled_by",
)

# Task fields concatenated into the prose the judge and the card vote read. Structured assignment
# never looks here, and neither step ever reads a field the task did not supply.
TASK_TEXT_FIELDS: Tuple[str, ...] = ("text", "title")

# Card fields that may name their persona outright, checked before the id/filename inference.
CARD_OWNER_FIELDS: Tuple[str, ...] = ("persona", "character", "rep_id", "owner", "persona_id")

# Upper bound on the UNTARGETED part of the card scan (see ``_candidate_card_paths``), so pointing
# ``cards_dir`` at a large directory degrades to "some cards considered" rather than a per-task
# filesystem sweep. Cards whose filename carries a persona's identity are found by targeted glob
# and are NOT subject to this cap.
MAX_CARD_FILES = 500

JUDGE_PROMPT = """A queued task is about to be run by an AI assistant. These personas are \
available (slug: role):
{roster}

Task text:
{text}
{linked}
Question: is the requester EXPLICITLY asking one of these personas to do this work (for example \
addressed to it, "as X ...", "have X do it", "ask X to ...")? A mere mention of a persona's name \
inside the task, or subject matter that merely overlaps a persona's domain, does NOT count.

Reply with ONLY a JSON object, no prose: {{"persona": "<slug>"}} if one persona is explicitly \
asked, else {{"persona": null}}."""

# Frontmatter + managed markers seeded into a freshly auto-registered persona skill. The
# runner-managed persona/learned blocks are filled by the next rep_sync pull (which writes between
# the QAR:MANAGED markers); only the frontmatter and a placeholder body ABOVE them are seeded, so
# the folder is a valid, invocable Claude skill immediately and human content is never clobbered.
SEED_SKILL_TEMPLATE = """---
name: {slug}
description: Act as the {display_name} AI representative.
user-invocable: true
---

# {display_name}

This skill represents the {display_name} AI representative. Its persona and learned corrections
below are managed by the quest-ai-runner rep sync and refreshed before each run; edit content
outside the managed markers freely.

<!-- QAR:MANAGED:persona START -->
<!-- QAR:MANAGED:persona END -->

<!-- QAR:MANAGED:learned START -->
<!-- QAR:MANAGED:learned END -->
"""


# --- the registry ---------------------------------------------------------------

@dataclass
class PersonaEntry:
    """One persona: the id Quest addresses it by, and the skill folder it runs as."""

    id: str                              # rep_id / user_id — what the Quest AI-profile calls it
    slug: str                            # skill directory name under ``skills_root``
    display_name: str = ""
    aliases: Tuple[str, ...] = ()

    def identifiers(self) -> Set[str]:
        """Every string this persona answers to, lowercased. Used for EXACT matching only."""
        out = {self.id, self.slug, self.display_name}
        out.update(self.aliases)
        return {str(v).strip().lower() for v in out if str(v).strip()}


@dataclass
class PersonaRegistry:
    """The personas a lane knows about, keyed by id. Empty is a perfectly valid registry."""

    entries: Dict[str, PersonaEntry] = field(default_factory=dict)

    # --- construction ---

    @classmethod
    def from_mapping(cls, data: Any) -> "PersonaRegistry":
        """Normalize either registry shape into entries. Unusable rows are skipped, never raised.

        A dict VALUE means the rich shape (key = persona name, ``rep_id`` = id, ``skill`` = slug);
        a string value means the flat shape (key = id, value = slug).
        """
        entries: Dict[str, PersonaEntry] = {}
        if not isinstance(data, dict):
            return cls(entries)
        for key, value in data.items():
            name = str(key or "").strip()
            if not name:
                continue
            if isinstance(value, dict):
                entry = cls._rich_entry(name, value)
            elif isinstance(value, str):
                slug = value.strip()
                entry = PersonaEntry(id=name, slug=slug) if slug else None
            else:
                entry = None
            if entry is None or not entry.id or not entry.slug:
                log.debug("persona registry: skipping unusable row %r", key)
                continue
            entries[entry.id] = entry
        return cls(entries)

    @staticmethod
    def _rich_entry(name: str, spec: Dict[str, Any]) -> Optional[PersonaEntry]:
        """Build an entry from the rich shape. The KEY is the persona's name, not its id."""
        ident = str(spec.get("rep_id") or spec.get("id") or spec.get("user_id") or "").strip()
        if not ident:
            # No explicit id: the name is the only handle we have, so address it by that.
            ident = name
        slug = str(spec.get("skill") or spec.get("slug") or name).strip()
        display_name = str(spec.get("display_name") or "").strip() or name
        aliases: List[str] = [str(a).strip() for a in (spec.get("aliases") or [])
                              if str(a).strip()]
        # The key is how a human refers to this persona; keep it matchable even when the file
        # also supplies a different display_name.
        if name.lower() not in {a.lower() for a in aliases} | {slug.lower(),
                                                               display_name.lower()}:
            aliases.append(name)
        return PersonaEntry(id=ident, slug=slug, display_name=display_name,
                            aliases=tuple(aliases))

    @classmethod
    def from_file(cls, path: Any) -> "PersonaRegistry":
        """Load a registry JSON file. A missing, unreadable or invalid file yields an EMPTY
        registry (logged) rather than an exception — a lane with no registry still runs."""
        if not path:
            return cls({})
        p = Path(str(path)).expanduser()
        try:
            if not p.exists():
                log.debug("persona registry file %s does not exist; no personas", p)
                return cls({})
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as e:
            log.warning("persona registry file %s is unreadable/invalid (%s); ignoring it", p, e)
            return cls({})
        registry = cls.from_mapping(data)
        log.debug("persona registry %s loaded (%d persona(s))", p, len(registry.entries))
        return registry

    # --- lookup ---

    def by_id(self, id: str) -> Optional[PersonaEntry]:  # noqa: A002 - part of the public contract
        """The entry addressed by this exact id, or None."""
        return self.entries.get(str(id or "").strip())

    def match(self, text: str) -> Optional[PersonaEntry]:
        """EXACT, case-insensitive match of a whole value against a persona's identifying strings.

        Never a substring or token scan: this is what keeps a task that merely mentions a persona's
        name from being routed to it. Only a field whose entire value IS the persona's id, slug,
        display name, or one of its aliases matches.
        """
        value = str(text or "").strip().lower()
        if not value:
            return None
        for entry in self.entries.values():
            if value in entry.identifiers():
                return entry
        return None

    def add(self, entry: PersonaEntry) -> PersonaEntry:
        """Register (or replace) an entry in place; returns it. Used by auto-registration."""
        self.entries[entry.id] = entry
        return entry

    def __bool__(self) -> bool:
        return bool(self.entries)


# --- resolver configuration -----------------------------------------------------

@dataclass
class PersonaResolverConfig:
    """Everything a lane's persona policy needs. Every step past the first is opt-in."""

    skills_root: str
    registry: Optional[PersonaRegistry] = None
    # Loaded into ``registry`` at build time when ``registry`` is None. Also the file an
    # auto-registered persona is persisted into, so its slug survives a restart.
    registry_file: Optional[str] = None
    assignment_fields: Sequence[str] = DEFAULT_ASSIGNMENT_FIELDS
    # Step 2: one cheap structured model call judging whether a persona was explicitly asked for.
    llm_explicit_ask: bool = False
    # Step 3: activation from the personas' domain cards.
    card_activation: bool = False
    cards_dir: Optional[str] = None
    card_min_hits: int = 2
    card_dominance: float = 2.0
    # Step 4: what to do with an id the registry does not know.
    auto_register: bool = False
    cache_dir: Optional[str] = None
    judge_tier: str = "fast"


# --- slug + skill-file helpers (auto-registration) ------------------------------

def slugify(text: str) -> str:
    """Turn a display name (or any string) into a filesystem/skill-safe slug. "" for empty input."""
    s = str(text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def unique_slug(base: str, ident: str, skills_root: Path,
                taken: Optional[Set[str]] = None) -> str:
    """A slug free of existing dirs under ``skills_root`` (and of an optional in-flight set).

    Falls back to a sanitized id when ``base`` is empty, then disambiguates with a short stable
    suffix from the id, then numeric suffixes. Never returns "".
    """
    candidate = base or slugify(ident) or "rep"
    taken = taken or set()

    def free(slug: str) -> bool:
        return slug not in taken and not (skills_root / slug).exists()

    if free(candidate):
        return candidate
    short = slugify(ident)[-6:] or "x"
    with_suffix = f"{candidate}-{short}"
    if free(with_suffix):
        return with_suffix
    n = 2
    while True:
        numbered = f"{candidate}-{n}"
        if free(numbered):
            return numbered
        n += 1


def seed_skill_file(skill_dir: Path, slug: str, display_name: str) -> None:
    """Create ``<skill_dir>/SKILL.md`` with valid frontmatter IF it does not already exist.

    Never overwrites an existing SKILL.md (human-authored content is preserved). Best-effort: a
    write failure is logged and the run proceeds, since the next sync still has a directory.
    """
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"
        if skill_path.exists():
            return
        skill_path.write_text(
            SEED_SKILL_TEMPLATE.format(slug=slug, display_name=display_name or slug),
            encoding="utf-8")
        log.info("seeded persona skill at %s (slug=%s)", skill_path, slug)
    except OSError as e:
        log.warning("could not seed persona skill at %s (%s)", skill_dir, e)


def _display_name_for(client: Any, ident: str, team_id: str) -> str:
    """Best-effort display name for an id, for the slug + skill description. Falls back to the id."""
    getter = getattr(client, "get_ai_profile", None) if client is not None else None
    if getter is None:
        return ident
    try:
        profile = getter(ident, team_id=team_id) or {}
        name = str(profile.get("display_name") or "").strip()
        if name:
            return name
    except Exception as e:  # noqa: BLE001 — a name lookup is never worth failing a task over
        log.info("display_name lookup for %s failed (%s); using the id", ident, e)
    return ident


def _persist_entry(registry_file: Optional[str], entry: PersonaEntry) -> None:
    """Merge a newly registered persona into the registry file. No-op when unset; never raises.

    The file's existing shape is preserved: a rich file gets a rich row (keyed by slug), a flat or
    empty file gets ``{id: slug}``.
    """
    if not registry_file:
        return
    path = Path(str(registry_file)).expanduser()
    try:
        current: Dict[str, Any] = {}
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        rich = any(isinstance(v, dict) for v in current.values())
        if rich:
            current[entry.slug] = {"rep_id": entry.id, "skill": entry.slug,
                                   "display_name": entry.display_name or entry.slug}
        else:
            if current.get(entry.id) == entry.slug:
                return  # already persisted
            current[entry.id] = entry.slug
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
        log.info("persisted persona %s -> %s in %s", entry.id, entry.slug, path)
    except (OSError, ValueError, TypeError) as e:
        log.warning("could not persist persona %s -> %s (%s)", entry.id, entry.slug, e)


def register_persona(ident: str, *, skills_root: Path, registry: PersonaRegistry,
                     client: Any = None, team_id: str = "",
                     registry_file: Optional[str] = None) -> PersonaEntry:
    """Register an unknown id as a real, invocable local skill and return its entry.

    Derives a slug from the persona's display name (else the id), makes it unique under
    ``skills_root``, creates the folder plus a seed SKILL.md with valid frontmatter, adds the entry
    to ``registry`` (so the same id reuses it for the rest of this process) and persists it to
    ``registry_file`` (so it survives a restart).
    """
    display_name = _display_name_for(client, ident, team_id)
    taken = {e.slug for e in registry.entries.values()}
    slug = unique_slug(slugify(display_name) or slugify(ident), ident, skills_root, taken)
    seed_skill_file(skills_root / slug, slug, display_name)
    entry = PersonaEntry(id=ident, slug=slug, display_name=display_name)
    registry.add(entry)
    _persist_entry(registry_file, entry)
    return entry


# --- the policy steps -----------------------------------------------------------

def _task_text(task: Dict[str, Any]) -> str:
    return " ".join(str(task.get(k) or "") for k in TASK_TEXT_FIELDS).strip()


def _first_field_value(task: Dict[str, Any], fields: Sequence[str]) -> str:
    """The first non-empty value among ``fields`` — the bare id for the structural fallback."""
    for key in fields:
        val = task.get(key)
        if val:
            return str(val).strip()
    return ""


def structured_assignment(registry: PersonaRegistry, task: Dict[str, Any],
                          fields: Sequence[str]) -> Optional[PersonaEntry]:
    """A persona named EXACTLY by a structured task field. No model call, no prose scanning."""
    for key in fields:
        entry = registry.match(task.get(key))
        if entry is not None:
            return entry
    return None


def _linked_names(task: Dict[str, Any], quest_client: Any) -> List[str]:
    """Names of the task's linked goal/quest, as extra context for the judge. Best-effort."""
    names: List[str] = []
    if quest_client is None:
        return names
    for key in ("goal_id", "quest_id"):
        linked_id = task.get(key)
        if not linked_id:
            continue
        record: Dict[str, Any] = {}
        for method_name in ("get_my_quest", "get_quest"):
            method = getattr(quest_client, method_name, None)
            if method is None:
                continue
            try:
                record = method(str(linked_id)) or {}
            except Exception:  # noqa: BLE001 — a lookup miss just means less context
                record = {}
            if record:
                break
        for field_name in ("name", "title", "outcome"):
            val = record.get(field_name)
            if val:
                names.append(str(val))
    return names


def _judge_model(provider: Any, tier: str) -> str:
    """Resolve the judge's model by TIER (never a hardcoded id). Falls back to the tier name."""
    try:
        from ..core.model_registry import ModelRegistry
        return ModelRegistry(provider).resolve_tier(tier or "fast")
    except Exception:  # noqa: BLE001 — an unresolvable registry must not stop the judge
        return tier or "fast"


def llm_explicit_ask(registry: PersonaRegistry, provider: Any, text: str,
                     linked_names: Optional[Sequence[str]] = None,
                     tier: str = "fast") -> Optional[PersonaEntry]:
    """Ask a model whether the requester EXPLICITLY asked a persona to do this work.

    There is no phrase or regex matching anywhere in this step: whether somebody was asked for is a
    judgment, made by one cheap structured call whose verdict is a JSON object
    ``{"persona": <slug>|null}``. Anything else (bad JSON, an unknown slug, a provider failure)
    counts as "not explicit" and returns None. Never raises.
    """
    if provider is None or not registry:
        return None
    try:
        roster = "\n".join(f"- {e.slug}: {e.display_name or e.slug}"
                           for e in registry.entries.values())
        linked = ""
        if linked_names:
            linked = "Linked goal/quest name(s): " + "; ".join(linked_names) + "\n"
        prompt = JUDGE_PROMPT.format(roster=roster, text=(text or "").strip(), linked=linked)
        raw = provider.answer([{"role": "user", "content": prompt}],
                              model=_judge_model(provider, tier))
        verdict = json.loads(_extract_json(raw or "") or "{}")
        if not isinstance(verdict, dict):
            return None
        return registry.match(verdict.get("persona") or verdict.get("character"))
    except Exception:  # noqa: BLE001 — a judge failure just means "not explicit"
        return None


def _tokens(text: str) -> Set[str]:
    return set(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _card_owner(card: Dict[str, Any], stem: str,
                registry: PersonaRegistry) -> Optional[PersonaEntry]:
    """Which persona a domain card belongs to, or None when it is nobody's or ambiguous.

    A card may name its persona outright in one of :data:`CARD_OWNER_FIELDS`. Otherwise ownership
    is inferred from the card's own identity (its ``id``, else its filename): a persona owns the
    card when one of its identifying strings appears there as a WHOLE token. Two personas matching
    the same card means the card is ambiguous, and ambiguity never activates anybody.
    """
    for key in CARD_OWNER_FIELDS:
        entry = registry.match(card.get(key))
        if entry is not None:
            return entry
    ident_tokens = _tokens(card.get("id") or "") | _tokens(stem)
    if not ident_tokens:
        return None
    owners = [e for e in registry.entries.values()
              if any(i in ident_tokens for i in e.identifiers())]
    return owners[0] if len(owners) == 1 else None


def _candidate_card_paths(cards_dir: Path, registry: PersonaRegistry) -> List[Path]:
    """Card files that could belong to a persona: targeted lookups first, then a bounded sweep.

    A plain ``sorted(glob("*.json"))[:MAX_CARD_FILES]`` is how this silently breaks. In a store of
    a few thousand cards, an alphabetical truncation simply never reaches a persona's card, and
    activation stops working with no error and nothing in the log -- the failure mode this repo's
    playbook bans. Worse, it depends on the names of unrelated cards, so it can start failing
    because somebody added cards elsewhere in the directory.

    So the identity-targeted glob comes first and is NOT capped: a card whose filename carries a
    persona's id, slug or alias is always found, no matter how large the store is. The untargeted
    sweep is kept on top of that, capped, only to catch cards that declare their owner in a FIELD
    rather than in the filename -- and it WARNS when it truncates, so a store that has outgrown the
    cap says so instead of quietly resolving fewer personas. Never raises.
    """
    paths: Dict[Path, None] = {}
    try:
        for entry in registry.entries.values():
            for ident in entry.identifiers():
                token = str(ident or "").strip().lower()
                if not token:
                    continue
                for path in cards_dir.glob(f"*{token}*.json"):
                    paths[path] = None
    except OSError:  # unreadable dir: fall through to the sweep, which handles it too
        pass
    try:
        every = sorted(cards_dir.glob("*.json"))
    except OSError:
        return list(paths)
    if len(every) > MAX_CARD_FILES:
        log.warning(
            "persona cards: %d files in %s exceeds the %d-file scan cap; cards that name their "
            "owner in a FIELD beyond the cap are not considered (cards whose filename carries the "
            "persona identity are always considered). Name persona cards after the persona, or "
            "point cards_dir at a smaller directory.",
            len(every), cards_dir, MAX_CARD_FILES)
    for path in every[:MAX_CARD_FILES]:
        paths[path] = None
    return list(paths)


def card_activation(registry: PersonaRegistry, text: str, cards_dir: Path,
                    min_hits: int = 2, dominance: float = 2.0) -> Optional[PersonaEntry]:
    """Activate a persona whose DOMAIN CARDS clearly dominate the task's subject matter.

    Each persona's cards contribute their ``keywords`` that appear as whole tokens in the task
    text. The persona's own name, display name and aliases are excluded from the count, so a bare
    name mention scores nothing — naming somebody is not asking them. A persona wins only on clear
    dominance: at least ``min_hits`` distinct keyword hits AND at least ``dominance`` times the
    runner-up's. Ambiguity or weak overlap yields None. Never raises.
    """
    if not registry or cards_dir is None:
        return None
    try:
        tokens = _tokens(text)
        if not tokens:
            return None
        hits_by_id: Dict[str, Set[str]] = {}
        for path in _candidate_card_paths(cards_dir, registry):
            try:
                card = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(card, dict):
                continue
            owner = _card_owner(card, path.stem, registry)
            if owner is None:
                continue
            skip = set()
            for ident in owner.identifiers():
                skip.update(_tokens(ident))
            hits = {str(k).strip().lower() for k in (card.get("keywords") or [])
                    if str(k).strip().lower() in tokens
                    and str(k).strip().lower() not in skip}
            if hits:
                hits_by_id.setdefault(owner.id, set()).update(hits)
        if not hits_by_id:
            return None
        ranked = sorted(((pid, len(h)) for pid, h in hits_by_id.items()),
                        key=lambda kv: (-kv[1], kv[0]))
        best_id, best_hits = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0
        if best_hits >= min_hits and best_hits >= dominance * runner_up:
            return registry.by_id(best_id)
        return None
    except Exception:  # noqa: BLE001 — card activation is best-effort by design
        return None


# --- the resolver ---------------------------------------------------------------

def build_persona_resolver(cfg: PersonaResolverConfig, *, provider: Any = None,
                           quest_client: Any = None, team_id: str = "",
                           on_resolved: Optional[Callable[[Optional[Dict[str, Any]]], None]] = None,
                           ) -> Callable[[Dict[str, Any]], Optional[Tuple[str, str]]]:
    """Compose a ``rep_sync_resolver``: ``task -> (id, skill_dir) | None``, from config alone.

    Args:
        cfg:          the lane's policy (which steps are on, where skills and cards live).
        provider:     a ``ModelProvider`` — required by, and ONLY used by, the explicit-ask judge.
        quest_client: a Quest client — used to name an auto-registered persona
                      (``get_ai_profile``) and to give the judge the linked goal/quest names.
        team_id:      fallback team for those lookups when the task carries none.
        on_resolved:  called with ``{"task", "user_id", "skill_dir"}`` on every successful
                      resolution and with ``None`` when nothing resolved. This is how a consumer
                      recovers WHICH persona a run is for (e.g. by stashing it on a thread-local
                      its context assembler reads back); the library deliberately keeps no such
                      state of its own. A raising callback is logged and ignored.

    The returned callable never raises.
    """
    registry = cfg.registry
    if registry is None:
        registry = PersonaRegistry.from_file(cfg.registry_file)
    skills_root = Path(str(cfg.skills_root or ".")).expanduser()
    fields = tuple(cfg.assignment_fields or DEFAULT_ASSIGNMENT_FIELDS)
    cards_dir = Path(str(cfg.cards_dir)).expanduser() if cfg.cards_dir else None
    cache_dir = Path(str(cfg.cache_dir)).expanduser() if cfg.cache_dir else None
    use_judge = bool(cfg.llm_explicit_ask and provider is not None)
    use_cards = bool(cfg.card_activation and cards_dir is not None)

    def notify(payload: Optional[Dict[str, Any]]) -> None:
        if on_resolved is None:
            return
        try:
            on_resolved(payload)
        except Exception as e:  # noqa: BLE001 — a consumer callback must not break a task
            log.info("persona on_resolved callback raised (%s); ignoring", e)

    def resolved(task: Dict[str, Any], ident: str, skill_dir: Path,
                 via: str) -> Tuple[str, str]:
        log.info("persona resolver: %s -> %s via %s", ident, skill_dir, via)
        notify({"task": dict(task), "user_id": ident, "skill_dir": str(skill_dir)})
        return (ident, str(skill_dir))

    def resolver(task: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        try:
            task = task or {}
            entry = None
            via = ""
            if registry:
                entry = structured_assignment(registry, task, fields)
                via = "structured field"
                text = _task_text(task) if (use_judge or use_cards) else ""
                if entry is None and use_judge and text:
                    entry = llm_explicit_ask(registry, provider, text,
                                             _linked_names(task, quest_client), cfg.judge_tier)
                    via = "explicit ask (LLM-judged)"
                if entry is None and use_cards and text:
                    entry = card_activation(registry, text, cards_dir,
                                            cfg.card_min_hits, cfg.card_dominance)
                    via = "domain cards"
            if entry is not None:
                return resolved(task, entry.id, skills_root / entry.slug, via)

            # Step 4 — the registry knows nobody by that id (or knows nobody at all).
            ident = _first_field_value(task, fields)
            if not ident:
                log.debug("persona resolver: no persona for this task")
                notify(None)
                return None
            if cfg.auto_register:
                new_entry = register_persona(
                    ident, skills_root=skills_root, registry=registry, client=quest_client,
                    team_id=str(task.get("team_id") or team_id or ""),
                    registry_file=cfg.registry_file)
                return resolved(task, ident, skills_root / new_entry.slug, "auto-registered")
            if cache_dir is not None:
                return resolved(task, ident, cache_dir / ident, "per-id cache dir")
            log.debug("persona resolver: %s is not a known persona", ident)
            notify(None)
            return None
        except Exception as e:  # noqa: BLE001 — a resolver failure must never break a task
            log.warning("persona resolver failed (%s); running without a persona", e)
            notify(None)
            return None

    return resolver
