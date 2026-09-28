# Link guard: no reply leaves with a link nobody checked

A model writes a URL that reads perfectly and does not exist. That is not a rare edge: it is what
language models do when a plausible address is easier to produce than a real one. The cost lands on
the reader, who taps it, gets nothing, and then stops trusting the links that *were* real.

The link guard (`quest_ai_runner/core/link_guard.py`) closes that. Every terminal reply is scanned
before it is emitted, and no link that could not be verified reaches the reader.

## Where it runs

Inside `Orchestrator.run`'s `finish()`, and deliberately as the last thing that touches the words:
before the `EVENT_RESULT` the consumer renders, and before the `ContextAssembler` write-back, so
what the reader sees, what the consumer persists, and what the assembler learns from are the same
sanitized text. It applies to both terminal kinds that carry prose (`answer` and `deep`).

## The three verdicts

| verdict | what it means | what happens |
| --- | --- | --- |
| `ok` | proven to exist | the link is left exactly as written |
| `dead` | proven not to exist | stripped, with "(link removed: that address does not exist)" |
| `unknown` | could not be settled | stripped, with "(link removed: could not verify it)" |

A link is `ok` when it is an external URL that answered (2xx/3xx, or 401/403, which prove the
endpoint is there and is merely gated), a host on the configured trusted list, an in-app
pseudo-scheme the consumer declared, or an internal path that matches the host app's real route
table. It is `dead` on 404/410, a host that does not resolve, a refused connection, or an internal
path with no matching route.

**Unverified is treated like dead on purpose.** A link nobody can stand behind is not worth sending,
and a short honest note beats a dead tap. The author's own label survives as plain text, so the
sentence still reads; only the address is gone.

## Multiple passes, which is the part that matters

One network reading is noisy, and a single slow host would delete a perfectly good link. So:

1. an `unknown` is re-checked up to `passes` times before the verdict sticks, and
2. after the text is rewritten it is **scanned again**, and the whole process repeats until a scan
   finds nothing left to strip (a fixed point, bounded by the same `passes`).

The fixed point is what makes the guarantee about the *emitted string*, not merely about the links
the first pass happened to see.

Every verdict is cached per URL for `cache_ttl` seconds on the guard instance, which lives as long
as the orchestrator does, so a long-lived poller checks a given URL once an hour, not once a reply.

## Configuring it

Two environment variables (both read in `cli.py`'s `_config_from_env`, both also settable as
`OrchestratorConfig.link_guard` / `.link_policy_file`):

- `QAR_LINK_GUARD`: `"0"` turns the guard off. **On by default**, because sending a fabricated
  address is worse than sending none.
- `QAR_LINK_POLICY_FILE`: a JSON file saying what "verified" means for your app.

```json
{
  "routes_file": "../my-app/app-routes.json",
  "routes": ["/extra/route/[id]"],
  "origins": ["app.example.org"],
  "trusted_hosts": [],
  "allowed_schemes": ["app-task"],
  "rewrites": [
    { "match": "^https://api\\.example\\.org/api/tasks/(atask_[0-9a-f]{12})$",
      "replace": "/profile/ai-tasks?taskId=\\1" }
  ],
  "check_external": true,
  "timeout": 4.0,
  "passes": 3,
  "max_urls": 25,
  "cache_ttl": 3600
}
```

- **`routes` / `routes_file`**: the route patterns your app actually has, in expo-router style
  (`/quest/[questId]/chat`, `/events/[slug]/[...rest]`). `routes_file` is resolved relative to the
  policy file and accepts either a bare list or a `{"routes": [...]}` object, so you can point it at
  a table your app GENERATES and never let the list go stale. **With no route table, an internal
  path has nothing to be checked against, comes back `unknown`, and is stripped.**
- **`origins`**: hosts that ARE your app. An absolute URL on one of these is judged by the route
  table, not fetched, because a single-page app answers 200 for paths it has no screen for, so an
  HTTP status proves nothing there.
- **`trusted_hosts`**: accepted without a network call. Every entry is a promise nobody re-checks,
  so keep it short (slow, rate-limited, or HEAD-hostile hosts you know are good).
- **`allowed_schemes`**: in-app pseudo-schemes your renderer handles itself. Never fetched.
- **`rewrites`**: `(regex, replacement)` pairs applied BEFORE judging. This is how a known-wrong
  address the model keeps writing becomes the right one instead of merely being deleted.

## What is not a link

URLs inside fenced code blocks and inline code spans are being *shown*, not offered, and are never
touched. Markdown links, angle autolinks (`<https://...>`), and bare URLs all are.

## Keeping it generic

This module knows nothing about any one app. The route table, the origins, the trusted hosts, the
schemes and the rewrites are all consumer-supplied data (hard rule #2). The single network seam is
`LinkGuard(fetcher=...)`, which is how the whole test suite runs offline.
