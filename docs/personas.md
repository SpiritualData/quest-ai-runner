# Personas: running a task AS somebody, from config

A lane that runs queued Quest tasks usually wants each task to run **as somebody** — an AI rep
standing in for a team member, or a named character with its own domain. The library already syncs
that identity: give `RunnerConfig` a `rep_sync_resolver` (`task -> (id, skill_dir) | None`) and the
poller pulls that persona's Quest AI profile into the local `SKILL.md` before the run and injects
its persona + learned corrections into the deep run.

What the resolver seam did **not** give you was anything to build a resolver *with*. So every lane
wrote the same machinery by hand: a registry mapping ids to skill folders under a skills root, plus
a policy for choosing one. `quest_ai_runner.runner.personas` is that machinery, and the policies are
configuration:

```python
from quest_ai_runner.runner import (
    PersonaResolverConfig, build_persona_resolver,
)

cfg.rep_sync_resolver = build_persona_resolver(
    PersonaResolverConfig(skills_root=SKILLS_ROOT, registry_file=REGISTRY_FILE),
    provider=provider, quest_client=client, team_id=TEAM_ID,
)
```

## The registry

A registry maps an **id** (what Quest addresses the persona by: a `rep_id` or a `user_id`) to a
**slug** (the skill directory name under `skills_root`). `PersonaRegistry.from_file` reads a JSON
file in either of the two shapes real lanes use, deciding by the value type:

```jsonc
// rich: the KEY is the persona's name, the id comes from rep_id, the folder from skill
{
  "sage":  {"rep_id": "<id>", "skill": "sage", "display_name": "Sage the Guide",
            "aliases": ["the guide"]},
  "scout": {"rep_id": "<id>", "skill": "scout"}
}

// flat: the KEY is the id, the VALUE is the folder
{"<user id>": "alpha-rep", "<user id>": "beta-rep"}
```

Both normalize to the same `PersonaEntry(id, slug, display_name, aliases)`. In the rich shape the
key stays matchable (as an alias) even when a different `display_name` is given, and
`display_name` falls back to the key. A missing, unreadable or invalid file yields an **empty**
registry, logged — never an exception, because a persona problem must never stop a task running.

`PersonaRegistry.match(value)` is **exact and case-insensitive, never a substring**: a field whose
whole value is the persona's id, slug, display name or one of its aliases matches; a sentence that
merely contains the name does not. That rule is load-bearing — see below.

## The four steps

`build_persona_resolver` composes the enabled steps and returns on the first hit. A step whose flag
is off is never entered, so a lane pays nothing (no model call, no file read) for a policy it did
not ask for.

1. **Structured assignment** — a task field in `assignment_fields` whose value matches the registry
   exactly. No model call. The default field list is the union real lanes use, most specific first
   (`assignee_rep_id`, `assignee_user_id`, `assignee`, `assigned_to`, `rep_id`, `user_id`,
   `persona`, `character`, `handled_by`), so the rep a task is *for* beats whoever filed it.
2. **Explicit ask, LLM-judged** (`llm_explicit_ask=True` and a `provider`) — one cheap structured
   call on `judge_tier` decides whether the requester is *explicitly asking* a persona to do this
   work ("as X ...", "have X do it"). The verdict is JSON (`{"persona": "<slug>"|null}`); bad JSON,
   an unknown slug or a provider failure all mean "not explicit". No phrase or regex matching
   anywhere: whether somebody was asked for is a judgment, so a model makes it.
3. **Domain-card dominance** (`card_activation=True` and a `cards_dir`) — content-based activation
   from the personas' own domain cards. Each card contributes the `keywords` that appear as whole
   tokens in the task text; a persona wins only on clear dominance (at least `card_min_hits`
   distinct hits AND at least `card_dominance`× the runner-up). A card belongs to a persona either
   by naming it outright (a `persona`/`character`/`rep_id`/`owner` field) or by carrying that
   persona's identity as a whole token in its `id`/filename; a card two personas could claim is
   ambiguous and counts for neither.
4. **Structural fallback** — an id read from `assignment_fields` that the registry does not know:
   with `auto_register` it becomes a real, invocable skill (below); else it resolves to
   `cache_dir/<id>` so the profile pull still has somewhere to land; else nothing resolves and the
   task runs as the plain assistant.

**A bare name mention never activates a persona.** It is the one rule all three activation paths
enforce together: matching is exact (step 1), the judge is told a mention is not an ask (step 2),
and each persona's own name, display name and aliases are excluded from its card scoring (step 3).
Naming somebody is not asking them, and a lane that routes on mentions routes wrongly and
confidently.

## Auto-registering an unknown persona

With `auto_register=True`, an id nobody has a skill folder for becomes one: the display name comes
from `quest_client.get_ai_profile` (falling back to the id), it is slugified and made unique against
`skills_root`, and the folder gets a seed `SKILL.md` with valid frontmatter (`name`, `description`,
`user-invocable`) plus the empty `QAR:MANAGED` markers the next rep-sync pull fills in. An existing
`SKILL.md` is never overwritten. The new entry is added to the in-memory registry (so the same id
reuses it for the rest of the process) and persisted to `registry_file` in that file's existing
shape; with no `registry_file` set, persisting is a safe no-op and the slug simply does not survive
a restart.

## Knowing which persona a run is for

`on_resolved` is called with `{"task", "user_id", "skill_dir"}` on every successful resolution and
with `None` when nothing resolves. It exists because a consumer's `ContextAssembler` is called with
the caller's scope, not the task, so it cannot otherwise tell which rep a run is for. The library
deliberately keeps no such state itself — the callback lets the consumer own it (a thread-local
works, since the poller resolves and executes one task per worker thread):

```python
current = threading.local()
cfg.rep_sync_resolver = build_persona_resolver(pcfg, on_resolved=lambda p: setattr(current, "value", p))
```

A raising callback is logged and ignored, like everything else here: **the resolver never raises**.

## The two policies, side by side

A **structural lane** — the task already names its owner, so resolution is a lookup and costs
nothing else. Unknown reps become real skills:

```python
PersonaResolverConfig(
    skills_root=SKILLS_ROOT,            # <root>/<slug>/SKILL.md per rep
    registry_file=SLUG_MAP_FILE,        # {"<user id>": "<slug>"}
    auto_register=True,                 # unknown rep -> a real invocable skill
    cache_dir=REP_SKILL_CACHE,          # ...or, with auto_register off, a per-id folder
)
```

A **character lane** — the task is prose from a human, so a persona activates only when it is asked
for, and otherwise the plain assistant runs:

```python
PersonaResolverConfig(
    skills_root=SKILLS_ROOT,            # <root>/<slug>/SKILL.md per character
    registry_file=CHARACTERS_FILE,      # {"<name>": {"rep_id": ..., "skill": ..., "aliases": [...]}}
    llm_explicit_ask=True,              # "have X do it" -> X (needs a provider)
    card_activation=True,               # dominant domain cards -> that character
    cards_dir=CARDS_DIR,
    judge_tier="fast",
)
```

Both hand the result to the same seam:

```python
runner_cfg.rep_sync_resolver = build_persona_resolver(pcfg, provider=provider,
                                                      quest_client=client, team_id=TEAM_ID)
runner_cfg.rep_sync_direction = "pull"   # run AS the persona's CURRENT Quest self
```

## Reference

| Field | Default | Meaning |
|---|---|---|
| `skills_root` | *(required)* | root holding one `<slug>/` skill folder per persona |
| `registry` / `registry_file` | `None` | the personas; the file is loaded at build time when `registry` is unset, and is also where auto-registration persists |
| `assignment_fields` | the union above | task fields that may carry an assignment or a bare id |
| `llm_explicit_ask` | `False` | step 2 on (needs a `provider`) |
| `card_activation`, `cards_dir` | `False`, `None` | step 3 on (needs both) |
| `card_min_hits`, `card_dominance` | `2`, `2.0` | how clearly a persona's cards must win |
| `auto_register` | `False` | register an unknown id as a real skill |
| `cache_dir` | `None` | fallback folder for an unknown id when `auto_register` is off |
| `judge_tier` | `"fast"` | tier the explicit-ask judge resolves its model from |

See also: [Writing a consumer](writing-a-consumer.md) for the surrounding `RunnerConfig`, and
`tests/test_personas.py` for each rule above proven in isolation.
