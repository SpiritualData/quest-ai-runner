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

A link is `ok` when it is an external URL that answered (2xx/3xx, or a 401/403 that is itself a
PAGE: an HTML sign-in wall or a private document a person can open), a host on the configured trusted list, an in-app
pseudo-scheme the consumer declared, a `mailto:` or `tel:` address (never fetched, kept as-is), or
an internal path that matches the host app's real route table. It is `dead` on 404/410, a host that
does not resolve, a refused connection, a 401/403 that is not HTML, or an internal path with no
matching route.

**"Verified" means a page a person can open.** A JSON (or other non-HTML) 401/403 is an API
answering "who are you", not a page: it is `dead` ("that address is not a web page"). This is the
exact shape of the fabricated `/api/tasks/<id>` links the guard was written for. The probe reads the
content type of a real GET (a 401 on HEAD is retried as GET for that reason), and a consumer's own
`fetcher` may return `(status, detail, {"content_type": ...})`; a fetcher that reports no content
type keeps the earlier trusting reading.

## Provenance: the AI must have seen the link

Reachable is not real: a model can invent an address that happens to resolve. Set
`"require_provenance": true` in the policy (off by default in the library) and an external link is
`ok` only if it is both reachable AND present in material this turn actually had: the user's
messages and the conversation, the context shown to the planner and answerer (cards, recalled turns,
the consumer's grounding such as quest data), every gathered observation (reads, greps, web searches,
`web_page` fetches), attachments, and the persona preamble. Comparison ignores case, scheme,
a leading `www.`, fragments and trailing slashes, and a query string must match. A Google Docs,
Sheets, Slides, Forms or Drive address is compared by its document id, so `/d/<ID>/edit`, `/d/<ID>`,
`/view` and `open?id=<ID>` all match each other. A host that was seen
does not vouch for an invented path on it. An unseen link is never fetched; it is stripped with
"I did not find this address in anything I read". In-app links (route table), `mailto:`, `tel:` and
declared schemes are exempt. A link that passes provenance but whose host answers `unknown` (429 rate limit, 5xx, timeout, a bot wall) is kept: it came from this turn's own search or page reads, and an unsettled check does not prove it absent. Only `dead` (404/410, no DNS, refused, not a page) strips a seen link. **Model output is never evidence.** A real Claude Code deep worker's
output is exempt from the rule, since what that worker read is not visible to the brain
(reachability and the route table still apply), and it counts as evidence. A runner that answers
in-process (`uses_deep_model = False`) is model-written: `DeepResult.model_written` is set by the
orchestrator, its output is judged against the turn's evidence plus `DeepResult.read_evidence` (what
that runner itself read, such as the results of the queries it executed), and its output never
vouches for anything. A consumer can add material the turn had that the run does not hold (the
whole user history past a transcript cap, pending proposals) as `context_meta["link_evidence"]`. The orchestrator builds the
evidence from the run itself (`link_evidence_for`) and passes it to `sanitize(text, evidence=...)`.
A consumer that builds its policy in code sets `OrchestratorConfig.link_policy` (a `LinkPolicy`),
which wins over `link_policy_file`.

**Unverified is treated like dead on purpose.** A link nobody can stand behind is not worth sending,
and a short honest note beats a dead tap. The author's own label survives as plain text, so the
sentence still reads; only the address is gone.

## Checking the ids in an in-app link

A route table proves a screen exists, not that `/quest/<id>` names something the reader can open. Set
`LinkPolicy.route_validator` (code only) to a callable `(route_pattern, params) -> verdict`, called
for a matched route that has bracket parameters, with the pattern as written in `routes` and the
concrete values (`{"questId": "..."}`). Return `None`/`True`/`"ok"` to keep the link, `False`/`"dead"`
or `("dead", "reason")` to strip it, `"unknown"` when undecided. A raising validator is `unknown`.

## Multiple passes, which is the part that matters

One network reading is noisy, and a single slow host would delete a perfectly good link. So:

1. an `unknown` is re-checked up to `passes` times before the verdict sticks, and
2. after the text is rewritten it is **scanned again**, and the whole process repeats until a scan
   finds nothing left to strip (a fixed point, bounded by the same `passes`).

The fixed point is what makes the guarantee about the *emitted string*, not merely about the links
the first pass happened to see.

Every verdict is cached per URL for `cache_ttl` seconds on the guard instance, which lives as long
as the orchestrator does, so a long-lived poller checks a given URL once an hour, not once a reply.

## Checking links does not mean checking them one at a time

A reply can carry up to `max_urls` links, each worth up to `passes` x `timeout` seconds in the
worst case; checked back to back that is minutes, sitting between the answer and the reader. So
`sanitize()` checks up to `LinkPolicy.concurrency` links at once (a thread pool; the one network
seam, `fetcher`, is still called per URL, so tests stay offline), and the WHOLE call is held to
`LinkPolicy.overall_timeout` (default 8s) rather than each link individually. Whatever has not
settled when that budget runs out is recorded `unknown` and stripped, same as any other link the
guard could not stand behind: the reply ships on time rather than waiting on a straggler.

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
  "cache_ttl": 3600,
  "concurrency": 8,
  "overall_timeout": 8.0,
  "require_provenance": false
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
