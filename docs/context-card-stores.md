# Context card stores: identity, mounting, moving, access

> **The Quest backend is now the default card and vector store.** A lane with Quest credentials
> (`QAR_QUEST_API_URL`|`QUEST_BASE_URL` and `QAR_QUEST_API_KEY`|`QUEST_API_KEY`) keeps its cards in
> quest-backend, which embeds each card once and searches server-side; the lane embeds and indexes
> nothing locally. **`QAR_CARDS_BACKEND=local` (alias `file`) is the opt-out.** Everything below about
> store identity, mounts, `merge-into`, `move-root` and read-only remote vectors describes the LOCAL
> backend and works exactly as written when a lane opts out. See
> [The Quest backend](#the-quest-backend-default) and [Uploading a local store](#uploading-a-local-store-context-upload).

What a card is (its fields, id rules, and what is not a card) is defined once in
[context-assembly.md, What a context card is](context-assembly.md#what-a-context-card-is).

How one card is stored, found, embedded and read when several corpora (lanes) overlap on disk. This
page is the design of record for store identity, read-only mounting, `context move-root` and
`context migrate-imports`, and it records the access model that was found in the code and the one
that replaced it.

Contents: [Where this came from](#where-this-came-from) · [Phase 1: how access works today](#phase-1-how-card-visibility-and-scoping-work-before-this-change) ·
[Containment audit](#containment-audit-defects-found-in-the-copy-import-path) · [The model](#the-model) ·
[Access](#access-what-the-process-can-read) · [Vectors](#vectors) · [Commands](#commands) ·
[Widening a corpus root](#widening-a-corpus-root-merge-into) ·
[A private reader of a shared vector server](#a-private-reader-of-a-shared-vector-server) ·
[Config keys](#config-keys) · [A person who also belongs to an org](#a-person-who-belongs-to-an-org-and-also-runs-a-personal-lane) ·
[The Quest backend](#the-quest-backend-default) · [Uploading a local store](#uploading-a-local-store-context-upload) · [Measured guarantees](#measured-guarantees) ·
[Migrating a deployment](#migrating-a-deployment)

## Where this came from

A corpus that contained (or sat inside) another bootstrapped store used to COPY that store's cards
under new namespaced ids (`<prefix>--<card id>`, with an `imported_from` provenance) and rewrite every
file path relative to the importing root (`FileContextStore._import_nested_cards`,
`_import_ancestor_cards`). Consequences:

- One card existed once per importing corpus and was fingerprinted, embedded and kept fresh by every
  one of them. The vector point id is `hash(scope digest | item id)` (`qdrant_vector_store._point_id`),
  so each re-namespaced copy was a NEW point, embedded again, and the old points were orphaned.
- Chained imports produced ids such as `<root>--<mid>--<name>` that grew a prefix per hop.
- A store's corpus root could not move: moving it meant new ids and new embeddings. There was no move
  operation.

## Phase 1: how card visibility and scoping work (before this change)

References are to the code as it stood before this change.

**The quest fence.** `executor.task_scope_tags` (`runner/executor.py:427`) tags every run with
`quest:<id>` for its `goal_id` and `quest_id`, and `executor.py:914` puts the tags into the run's
context meta. `scope_tags_allow` (`core/scope_tags.py:57`) is the one predicate: an item with no tags
is visible everywhere, a turn with no tags sees everything, otherwise the tag sets must intersect. It
is a post-filter applied by each arm: the keyword arm to its candidate pool
(`file_context_store.py`, "Cross-quest fence", `turn_scope_tags`), the vector arm to hits
(`vector_context_assembler.py:872`), past turns (`turn_context_store.py:131`), recent context
(`recent_context.py`), recipes and anticipation. Cards gain tags by UNION when a run touches them
(`update_card(scope_tags=...)`, `record()`), never by overwrite.

**The vector scope.** One collection per vector size; scope is a payload filter, not a collection
(`qdrant_vector_store._scope_hash` `:270`, `_visibility_filter` `:530`, `_exact_scope_filter` `:562`).
Visibility is "this scope OR unscoped OR the seeded-card partition". `CARD_SEED_SCOPE`
(`core/vector_scopes.py`) puts embedded cards in a partition that is visible under every scope. Two
findings here:

1. `VectorContextAssembler._assemble_inner` passes the WHOLE context meta as the scope
   (`vector_context_assembler.py:858`, `scope = meta or None`), whose digest matches nothing a writer
   stored, so in practice the arm sees only unscoped and seeded points.
2. `record()` writes task associations with `scope = outcome.get("scope")` (`:464`); no caller in this
   repository sets it, so associations are written UNSCOPED, i.e. visible to every search on that
   collection, and the association payload carries no `scope_tags` (`:520-527`), so the quest fence
   in step 1 above never applies to them.

**Writer / owner fields and the vector floor.** A card's `provenance` records who wrote it
(`created_by_task`, `task_id`); the keyword arm drops a card recorded from the run's own thread
(`own_task`). Cards a consumer manages declare `managed_fields` / `managed_items`. The vector arm has
a per-hit confidence floor (`_hit_min_score`).

**Per-rep context.** `Poller._rep_context_dirs` (`runner/poller.py:953`) puts a rep's notes and turns
under `<cards_dir>/reps/<user id>/`; the rep's learned `context_prefs` live on the rep's Quest
profile (`adapters/rep_context_assembler.py`, keyed by `personas.current_rep`). These are per rep.
The cards themselves are not.

**Repositories.**
- `FilesystemCardRepository` (`card_repository.py`): one directory, no notion of user or team. Whoever
  can read the directory reads every card.
- `QdrantCardRepository` (`qdrant_card_repository.py`): hard-scoped by a consumer-chosen payload dict
  (`_scope_filter` `:230`, `_in_scope` `:252`). Scope by user and members are isolated; scope by team
  and they share.
- `QuestApiCardRepository` (`quest_api_card_repository.py`): `X-User-Id` header (`:205`) and the
  listing `/api/cards` (`load_all` `:292`); writes are enriched with `team_id` and `runner_env_id`
  (`_enrich_card`). What a user sees is decided by quest-backend, not here.

**Concrete answers.**

- *Can context for team member B use cards created during member A's run on the same team?* Yes when
  both run in the same lane with a filesystem or team-scoped Qdrant store: cards are written to the
  lane's one store (`record()`, the post-deep updater) with the run's quest tags, and any later run
  whose tags intersect (or that has none) can select them. There is no per-member partition of cards;
  only the per-rep notes, turns and prefs above are per member. With `QuestApiCardRepository` it
  depends on quest-backend (the listing is per `X-User-Id`). With a user-scoped `QdrantCardRepository`
  B does not see A's cards.
- *Within one org, across teams?* The same store is shared by every run in a lane; the boundary
  between teams is the lane (its `cards_dir`, and `claim_teams` limiting which tasks it runs), not
  anything in the store. Inside a lane the only fence is the quest tag.
- *What stops a run for one quest seeing another quest's cards?* Only `scope_tags`, applied as a
  post-filter, and only where the item carries tags. Untagged cards and task associations pass.
- *How does a lane of a different org or team (a personal lane whose corpus root contains an org's
  folder) get that org's cards today?* By COPY: `_discover_nested_card_dirs` finds the org's
  `.quest-context` under the root and `_import_nested_cards` reads its JSON files directly. The only
  enforcement is the operating system's: if the lane's user can read the directory, it gets the cards.
  The store's contents are then re-owned by the importer (its own cards, its own embeddings), so the
  org can neither see nor revoke them. Nothing in the library checks who the importer is.
- *Over a shared Qdrant server?* Seeded cards are visible under every scope and associations are
  unscoped, so any lane that can reach the same server and collection can retrieve every other lane's
  card embeddings and task memory. The filesystem enforces nothing there, and neither did the store.

## Containment audit: defects found in the copy-import path

`file_context_store.py` unless noted. Lines are as of this change.

| # | Defect | Where | Status |
|---|---|---|---|
| 1 | Imported ids are `<prefix>--<card id>` truncated to 200 characters; in a chain the prefixes stack and a truncation can make two different cards collide | `:3333`, `:3439` | Replaced by mounts (identity is `(store_id, card_id)`, ids are never rewritten). Remains in `copy` mode |
| 2 | Same-direction chains are deliberately kept, so a copy of a copy is re-imported under another prefix, re-fingerprinted and re-embedded | `:3325`, `:3431` | Replaced by flat mounting: every store around the corpus is mounted directly, copies are skipped (`view_card`) |
| 3 | Discovery stops at the first nested store (`dirnames[:] = []`), so a store beneath it is reachable only through the nested store's own copies, which may be stale | `:3230` | Replaced: mount discovery is flat (`discover_nested_stores`) |
| 4 | **Permission errors are swallowed.** `(dir / ".quest-context" / bootstrap_meta.json).is_file()` is False when the directory is unreadable, so an unreadable store is not recognised as a store at all and its files are walked and re-carded by the importer; `except Exception: pass` hides the rest | `:3228`, `:3233`, `:3364`, `:3370` | Fixed for mounting: `claims_subtree` counts an unreadable card directory as a store, `check_readable` returns the reason, and the mount layer logs it. Remains in `copy` mode |
| 5 | **A transient read failure deletes the imported cards.** `FilesystemCardRepository.load_all` returns `{}` on error and skips unreadable files (`card_repository.py:230`); `_import_*` then yields nothing and the prune loop deletes every previous copy whose id is not in the new set, so a permission flip or torn read wipes a lane's context, and the next good read re-imports and re-embeds all of it | `:3679-3700` | Replaced: a mount holds no copy to wipe. Remains in `copy` mode |
| 6 | Stale imports are only pruned when a bootstrap pass runs; between passes a copy outlives the card it was copied from. `_imported_card_unchanged` only avoids rewrites, it does not detect a source change until the next pass | `:3237`, `:3679` | Replaced: a mount reads the owner's current cards (`MountedCardRepository.revision`) |
| 7 | Excluded folders are honoured for a NESTED store (`:3663`) but not for an ANCESTOR import, whose files are only filtered to lie under the root. Files in excluded or skipped folders re-enter through the ancestor and undo the prune | `_import_ancestor_cards` `:3374-3440` | Replaced for mounts (the mounted view is read, not merged into this corpus's coverage). Remains in `copy` mode |
| 8 | An imported card carries paths but not fingerprints, so every importing corpus re-hashes (`git hash-object`) every file of every imported card | `:3891` onward | Replaced: the owner's cards are read as they are |
| 9 | `_folder_review` decides "this nested repository has its own store" with `.exists()` on a path inside the store, which is False for an unreadable store, so it logs "bootstrap them separately" for a store that exists | `:762` | Remains in `copy` mode (in `mount` mode the walk is skipped by `skip_subtrees` before the review runs) |
| 10 | `bootstrap_report_state.json` was enumerated as a card (it was missing from the sidecar list), counted in `card_count` and scored for keyword matches | `card_repository.py:47` | **Fixed** |
| 11 | Task associations carry no `scope_tags` and are written unscoped, so the quest fence never applies to them and they are served to every search on the collection | `vector_context_assembler.py:464`, `:520` | Fenced by store in `mount` mode (they are attributed to the writing lane's store and visible to nobody else). The quest fence gap itself remains: a follow-up should stamp `scope_tags` on the association payload |
| 12 | The vector arm's `scope` argument is the whole context meta, which matches no stored digest | `vector_context_assembler.py:858` | Not changed; the store fence does not depend on it. Worth a follow-up |
| 13 | A vector point is keyed by item id alone within a scope, so two stores that share a collection and share a card id overwrite each other's point | `qdrant_vector_store._point_id` `:283` | Fixed for new stores (`vector_id_scheme: qualified` keys points `card:<store_id>:<card_id>`); stores that already have points keep their ids (`legacy`) so nothing is re-embedded |
| 14 | Locks, refresh stamps, reps, turns, recent, predictions and the embedded Qdrant all live under the lane's own `cards_dir` (`config.py:1677` `index-state/last_refresh`, the `.bootstrap.lock` beside it), so a lane writes into another store only when two lanes are configured with the SAME `cards_dir`. None of the import paths write into the source store | `config.py:1677-1760` | Verified; no defect in the import paths. Two lanes sharing a `cards_dir` share all of it, which the mount model makes unnecessary |

## The model

**Store identity.** Every store has a `store_id` recorded once in its `bootstrap_meta.json`
(`card_store_identity.py`), generated randomly and never derived from a path. Card identity is
`(store_id, card_id)`. A card's file paths are relative to ITS store's root and are never rewritten.
The meta also records the store's `root` (where those paths resolve from), a `root_history`,
the `mounts` it reads, a `vector_id_scheme`, an optional `vector_space`, an optional
`vector_location` (where its points are published, see [below](#a-private-reader-of-a-shared-vector-server))
and, after a merge, `merged_from` (on the wide store) or `merged_into` (on the source). A bootstrap pass rewrites
only the bootstrap keys and carries these through; it never moves a recorded root.

**Mount instead of copy.** With `QAR_CARD_MOUNT=mount` a corpus that contains (or sits inside) other
stores reads them in place and read-only (`hubofhubs.recall.card_store_mounts`):

- A mounted card appears as `"<store_id>:<card_id>"`. Its paths are resolved against the two real
  roots at read time (`rebase_path`), so an ancestor's `lane/a.py` is the reader's `a.py`, and a path
  outside the reader's root is dropped. Nothing is written.
- The reader's bootstrap/refresh skips the mounted subtree entirely: `skip_subtrees` makes every
  directory walk in the pass (folder review, nested-repo detection, the file walk) stop at it. No file
  walked, no card made, no staleness check, no fingerprint, no embedding.
- The reader never keeps a snapshot. `MountedCardRepository.revision()` includes the owner's own
  change marker (its directory stamp and its meta file), so the owner's next write is the reader's
  next read. Freshness of a mounted store is solely its owner's job. (The marker is a directory
  timestamp, with the repository's existing 30 second full re-scan as a backstop for two writes inside
  one timestamp tick.)
- If a mount is not readable or has no identity yet, the reader logs it and simply has no cards for
  that subtree. It does NOT index those files itself (that would recreate the duplicate). A store
  ABOVE the reader that cannot be mounted claims everything under the reader's root, so the pass
  indexes nothing and logs an error.
- Copies left by the old behaviour stay until `context migrate-imports` converts them; a mount-mode
  pass never deletes or silently skips them, it logs how many remain. Copy-import is opt-in only
  (`QAR_CARD_MOUNT=copy`, the default until a deployment migrates), and is never used as a fallback for
  a mount that is allowed.

**Write isolation.** The store only ever writes to its own directory. A write, delete, usage stamp or
card update addressed to a mounted card is refused (`MountedCardRepository.write/delete`,
`FileContextStore.write_card`, `_update_card_inner`, `_mark_sources_used_inner`). Everything a reading
lane produces (cards, turns, rep notes and prefs, locks, stamps, embeddings, task associations) goes
under its own `cards_dir` or its own `store_id`.

**move-root.** The recorded `root` is the only thing that changes. See [Commands](#commands).

## Access: what the process can read

There is no access list and no policy field. What a lane may mount is what its OS user can read: the
store must be within the corpus (nested) or an ancestor of the corpus root, AND `check_readable`
must succeed. It is a real read (list the directory, open and parse `bootstrap_meta.json`), so
permissions, ACLs and a mount that went away are what they are, and the failure is named in the log
and in `context store-info`. A policy in a file the reader can open would only pretend to restrict
what the disk allows. Reads from a mount are still subject to the usual quest fences (`scope_tags`).

A store ABOVE the corpus root that the process cannot read is invisible to it, not a block: the
lane indexes its own root as usual. This is the normal shape when a person's wider corpus sits above
an org's: the org's lanes cannot read the person's store, and the person's lane mounts the org's.

The vector server is the exception: it answers anyone who can reach it. Mount mode therefore fences
the vector arm (below).

## Vectors

In mount mode `StoreFencedVectorStore` wraps the lane's vector store:

- Card points are returned only for the reader's own `store_id` plus the stores it has actually
  mounted; a card point with no `store_id` is not served; the task associations a lane records are
  attributed to its own store and served to nobody else. Unattributed non-card points (associations
  from before the fence) are served only if `QAR_VECTOR_UNATTRIBUTED=1`, for a single-tenant store.
- The reader embeds nothing for a mounted store (`export_for_embedding` exports own cards only), so a
  card has one point however many corpora mount it. The owner's seed pass stamps `store_id` onto
  points embedded before the store had an identity with a payload write, never a re-embedding.
- `sync`/`upsert` drop any item attributed to another store, so a reader cannot write into a mounted
  store's points.
- `QAR_VECTOR_SPACE` / `context store-info --vector-space` declare which server/collection holds a
  store's points; the vector arm skips a mount only when both sides declare a space and they differ.
  Such a mount is served by the keyword arm alone.
- A mount whose owner recorded a `vector_location` on a DIFFERENT backend than the reader's own is
  not served through the reader's store at all: the reader queries those points read-only on that
  backend, never writes to it, and never copies or embeds the mount's cards locally. See
  [A private reader of a shared vector server](#a-private-reader-of-a-shared-vector-server). A
  reader whose stores live on another Qdrant server no longer has to stay on
  `QAR_CARD_MOUNT=copy`.

## Commands

Every command is a dry run unless `--apply` is given, prints what it would change, and embeds nothing.

```
quest-ai-runner context store-info --cards-dir DIR [--init] [--vector-space NAME] [--apply] [--json]
quest-ai-runner context move-root --cards-dir DIR --new-root PATH [--apply] [--force]
quest-ai-runner context migrate-imports --cards-dir DIR [--root PATH] [--apply]
        [--prune-points] [--qdrant-url URL | --qdrant-path PATH] [--collection-prefix PREFIX]
        [--vector-size N]
quest-ai-runner context merge-into --cards-dir NARROW --into WIDE [--apply] [--prune-source]
        [--qdrant-url URL | --qdrant-path PATH] [--collection-prefix PREFIX] [--vector-size N]
quest-ai-runner context store-info --cards-dir DIR --vector-location URL|embedded
        [--vector-collection-prefix P] [--vector-size N] [--embedder-id ID] [--vector-path DIR] --apply
quest-ai-runner context upload --cards-dir DIR (--team-id ID | --org-id ID | --private) [--apply] [...]
```

`upload` is described in [Uploading a local store](#uploading-a-local-store-context-upload).

- `store-info` shows the id, root, mounts, vector location, merge records and whether each mount is
  readable by this user; `--init` gives a store without an id one (it needs an existing
  `bootstrap_meta.json`; it never invents one). `--vector-location` is set by a store's OWNER (see
  below). Without `--vector-size`, a server location is probed read-only for the one vector size its
  prefix has.
- `merge-into` moves a store into a wider one with no re-embedding. See
  [Widening a corpus root](#widening-a-corpus-root-merge-into).
- `move-root` records a new root. Ids, vectors and relative paths are unchanged, so re-embeds are 0.
  The plan reports how many file references exist under the new root and refuses a missing target
  unless `--force`. The previous root is kept in `root_history`. Readers follow the move on their next
  read, because they derive the mount prefix from the owner's recorded root.
- `migrate-imports` finds the namespaced copies in a store, mounts the stores they came from (and every
  other readable store around it), records the mounts in `bootstrap_meta.json` FIRST, then deletes the
  converted copies, so a failure leaves a duplicate and never a gap. A copy whose source store is not
  mountable is kept and reported. With `--qdrant-url`/`--qdrant-path` it lists the orphan points the
  copies left, and with `--apply --prune-points` deletes them.

## Widening a corpus root: merge-into

A lane widens its corpus root from `X/sub` to its parent `X`. Its store `X/sub/.quest-context` has to
become part of the wider store `X/.quest-context` (which may already exist with its own cards and
identity): a mounted store needs an owner lane to stay fresh, and re-indexing `X/sub` would re-card
and re-embed everything.

`context merge-into --cards-dir X/sub/.quest-context --into X/.quest-context` is a dry run that
prints the cards to move, the id collisions, the files missing under the wide root, the state files
to copy, and the points to relabel. `--apply` does it. What it does, in order (a failure leaves a
duplicate, never a gap):

1. **Cards.** Each narrow card is written into the wide store with its id unchanged and every file
   path prefixed with the narrow root's position under the wide root (`a.py` becomes `sub/a.py`; the
   same for `content[].locator.path`). `provenance.merged_from` records the source store and card.
   Cards that are themselves copy-imports are not moved (mount the original). A wide store that does
   not exist yet is created, with an identity and the source's bootstrap bookkeeping.
2. **Vectors, with the embedder never called.** Each moved card's stored vector is re-used.
   - Both stores on the legacy scheme (point id derived from the card id): the point id does not
     change, so the point is rewritten in place with the wide store's `store_id` and the prefixed
     `paths`. This is the zero-copy case.
   - Any other combination (a qualified source, whose ids are `card:<store_id>:<card_id>`, or a
     renamed card): the point is read with its vector and written under the new point id with the new
     `store_id` and `paths`; the old point is deleted only with `--prune-source`. The wide store's
     `export_for_embedding` then yields the same ids and fingerprints, so its next seed pass re-embeds
     0 cards.
   - A card with no point in the source is simply not embedded here; the wide lane's seed pass does it
     as for any card. A renamed card in a legacy-scheme store is skipped when the source's point is
     really the wide store's own card's point (same point id); the wide lane embeds that one card.
3. **State.** Non-card state directories the narrow store holds (turns, per-rep notes, anything but
   vector directories, `index-state` and `_quarantine`) are copied to the same relative place in the
   wide store, never over an existing file. A file that exists in both with different content is
   reported as a collision and the wide file is kept; the source's copy stays in the source.
   Derived sidecars (`folder_review.json`, `discovered_files.json`) are not moved.
4. **Identity.** The wide store records `merged_from` (source store id, date, path prefix, counts).

**Id collisions never overwrite.** For a narrow card whose id exists in the wide store: if the wide
card carries `provenance.merged_from` for this source card, or has the same name and the same set of
file paths, it is the same card and the wide one is kept ("already present"). Otherwise the incoming
card is renamed to `<id>--m<first 8 hex of the source store id>` and moved. If that id is itself taken
by a different card the card is reported as unresolved and stays in the source. Re-running the command
therefore changes nothing (it does not even rewrite the meta file).

**The source.** Without `--prune-source` the source is left untouched and the command says so: it
still claims its subtree, and a reader (or a copy-mode wide lane) still sees two stores for the same
files. With `--prune-source`, the moved cards (and the state files that were copied) are removed from
the source, its `bootstrap_meta.json` records `merged_into`, and every discovery path (mount
discovery, `claims_subtree`, copy-mode nested and ancestor discovery, `check_readable`) treats it as
not a store. A wide store that held copy-imports of the source (copy mode) has them and their points
removed too, since the moved cards supersede them. Run the prune once the wide lane is verified; the
second run only prunes.

**Freshness afterwards.** The wide store's normal refresh treats the moved cards as its own: their
stored `sha256`/`mtime` are compared at the prefixed path, so an unchanged file is not re-carded
(`refresh_stale` with a provider that must not be called), and a changed one is stale at its new path.

**Operational requirements.** Stop the lanes that own the narrow and the wide store (and, for an
embedded Qdrant, any process holding it) before `--apply`: the relabel of an in-place point is not
atomic with the owner's own sync, and an embedded Qdrant allows one process. `--qdrant-url` reaches a
server (the vector size is detected, or `--vector-size`); `--qdrant-path` opens an embedded one.

Recipe, as commands (dry runs first):

```
quest-ai-runner context store-info --cards-dir X/sub/.quest-context --init --apply   # if it has no id
quest-ai-runner context merge-into --cards-dir X/sub/.quest-context --into X/.quest-context \
    --qdrant-url http://HOST:6333
quest-ai-runner context merge-into ... --apply                    # source untouched
# point the lane's corpus root at X, restart it, check its first seed pass embeds 0 cards
quest-ai-runner context merge-into ... --apply --prune-source     # now the narrow store is gone
```

## A private reader of a shared vector server

A reader lane keeps its OWN vectors in a private embedded Qdrant that only its OS user can read. A
store it mounts has its points on a Qdrant server that both the reader and the owner can reach. The
reader must query the mounted store's points there, and must never write anything (its own cards,
turns, associations) to that server, because other users can reach it.

- **The owner records where its vectors live**, once, in the store's identity:
  `context store-info --cards-dir DIR --vector-location http://HOST:6333 --apply` (or `embedded`).
  The record is `vector_location`: `location`, `collection_prefix`, `vector_size`, `embedder` (the
  model family, default from `QAR_EMBEDDER_BACKEND`) and, for `embedded`, the Qdrant `path`. A reader
  reads it from the mount; there is no reader-side copy of that configuration and nothing for the
  reader's operator to keep in step. The record is the lane's published location: it should name the
  backend the owner's lane actually syncs to.
- **The reader's vector arm** queries its own store through its own vector store, as before, and each
  mounted store whose recorded location differs from the reader's own backend (or collection prefix)
  through a read-only client to that location, filtered by `_scope` (the card partition) and by that
  `store_id`, which is the existing store fence. Results are merged into one ranking by score. A
  mount whose owner recorded no location, or the reader's own backend, is served exactly as before.
- **No write, structurally.** `ReadOnlyVectorReader` (`adapters/card_store_vectors.py`) is the only
  handle on that backend and has no upsert, delete, set_payload or create method; it calls only
  `collection_exists`, `get_collection(s)` and `query_points`. The reader's own `sync`/`upsert` go to
  its own store, and items attributed to another store are dropped as before.
- **Query embeddings must be compatible.** The reader embeds its QUERY with its own query embedder
  and sends the vector. Before any request it checks the vector length against the recorded
  `vector_size` and, when both sides name an embedder family, that they are equal. Incompatible or no
  embedder available: that mount's vector arm is skipped with one log line
  (`mounted store ...: vector arm skipped, ...`) and its cards are still served by the keyword arm.
- **Unreachable or embedded elsewhere.** A server that does not answer is logged once and left alone
  for 60 seconds at a time (no timeout on every turn). An `embedded` location in a directory that is
  not the reader's own is never opened (an embedded Qdrant takes a write lock on its directory, so it
  cannot be read without writing); the log says whether that directory is even readable by the
  reader. In every such case the mount's cards are served by the keyword arm only, and are never
  copied or embedded locally.
- **One embedding per query.** The query embedding is computed once and re-used for the reader's own
  store and every remote mount.

## The risk gate: folders that must not be read, whatever a model is available

Some folders are dangerous for a reason a file count never shows. The case that motivated this: a
machine's daily backup snapshots (one folder per date, each a copy of the whole tree) each carry their
own card store, so one backups folder held 155 stores and 760,608 cards, and a corpus rooted above it
would have walked, imported and mounted every one. `folder_gate` (`context_risk_gate.py`) is the one
function every card source asks, so there is one answer and no per-path copy of it: the file walk,
nested store discovery, ancestor imports, mounts discovery and the `migrate-imports` report.

- **Signals, no model, computed in one cheap walk** (`risk_signals` is a pure function): a folder whose
  subtree holds three or more card stores (one nested store is normal and is no signal); a series of
  sibling folders named like dates, timestamps or version numbers that share one child structure;
  a very large folder (the folder review's own `_FOLDER_REVIEW_MIN_FILES`); and a copy of the corpus
  itself. That last one is asked of EVERY folder the walk meets, whether or not it holds a card store: a
  folder whose own child folders repeat at least three of the corpus root's top-level folder names and 80% of
  them (the names on the way down to it are not counted) is a copy, and a dated or versioned name on its
  path is said too (`backup_2026-10-16`, taken without its card store, is caught by its layout alone). A
  folder with a card store is also a copy when it carries this store's own identity, or records this
  corpus's root while it sits under a dated name or beside a repeated layout. A folder is reported at the
  shallowest level that holds the pattern, and a confirmed series or copy is not walked into.
- **Always reviewed.** A folder with a signal is put to the folder review whatever the corpus size (the
  2,000-file floor does not apply), with the signal in the prompt (`[RISK: contains N card stores]`,
  `looks like a series of N dated or numbered copies`). The verdict is cached in `folder_review.json`
  with a reason and `source: risk_signal`, and stays human-editable: set `"index": true` on the folder
  to override a skip.
- **If the review cannot run** (`QAR_FOLDER_REVIEW=0`, no provider, a failed call), a folder with a
  stores, series or copy signal is not indexed, not descended into, and no store beneath it is imported or
  mounted. One warning per run names the folder, why, and the override. A very large folder alone is
  context for the model, never a block. Folders with no signal keep the fail-open behaviour: an
  unreviewable ordinary corpus is indexed in full.
- **Ancestors.** An ancestor's cards are trimmed of files in a turned-away folder, and an ancestor
  store that is itself one of a series of dated copies is ignored until it has a verdict (the key is
  the series' folder relative to the corpus root, such as `"../.."`).
- **`context migrate-imports`** shows folders the gate turned away (`RISK, NOT READ`); like the rest of
  the report it makes no model call and changes nothing.

## Config keys

All environment variables (in a lane's `qar.toml` `[env]` table or its unit environment):

| Key | Meaning | Default |
|---|---|---|
| `QAR_CARD_MOUNT` | `mount` reads readable neighbouring stores in place; `copy` is the legacy copy-import | `copy` |
| `QAR_VECTOR_SPACE` | name of the vector server/collection this lane uses (must match the owners' `vector_space` to query their points) | unset |
| `QAR_VECTOR_UNATTRIBUTED` | `1` serves pre-fence task associations that carry no `store_id` | off |
| `QAR_REUSE_NESTED_CARDS` | `0` turns off copy-import in `copy` mode (unchanged) | on |
| `QAR_MAX_CARDS` | the most cards one store owns, on any repository; `0` or `unlimited` for none (see [the card limit](#the-card-limit)) | `1000` |
| `card_tidy` (`qar.toml`, `RunnerConfig.card_tidy`) | what the automatic tidy does: `apply`, `plan` or `off` (see [tidying a store on any backend](#tidying-a-store-on-any-backend)) | `apply` on a local folder, `plan` on the Quest backend or Qdrant |

## The card limit

A store the runner builds holds at most `QAR_MAX_CARDS` cards (1,000 unless set; `0` or the word `unlimited`
for no limit). The number belongs to the runner: `FileContextStore(max_cards=...)` called from code applies
no limit unless its caller passes one, and the runner's builders (`config_assembler.py`, the `bootstrap`
command) pass `card_limit.max_cards_from_env()`. The quarantine folder, the sidecars and the turn files do not count. A limit like this was tried and
removed on 2026-10-05 because it refused to index without saying so and left a person to guess the number,
so this one never acts silently. It counts the cards the store OWNS on every repository: a local folder, the
Quest backend (where a lane counts the cards it wrote, not every card its key can read) and Qdrant all get the
notice and the breakdown below, and a full store adds no derived card on any of them. Only a local folder can
move a cold card aside for a learned one (the next paragraphs); on the backend a learned card is admitted over
the limit and counted. The limit never trims a store by itself: the [tidy](#tidying-a-store-on-any-backend) does.

**At the limit**, one function (`CardLimit.admit`) answers every writer:

- a card that already exists is refreshed in place, always;
- a new derived card (a topic card or an import, regenerable from the corpus) is not written. A store with
  room for N more cards asks the model about the N most recently changed folders only (those with enough
  files for a card first), never about every folder to have most of its cards refused; the other files
  wait, not "seen", and raising the limit (or a card moving out) indexes them. A full store asks the model
  nothing;
- a new card learned from a run or written for a quest, in a store that holds exactly its limit, is admitted
  by moving the least valuable derived card out (unused first, then least recently touched) to
  `_quarantine/limit/`, with a line in `manifest.jsonl` there. Nothing is deleted. Only a card an index pass
  wrote and nobody has touched can move (`card_origin.protection`): never one with notes or edits, quest
  tags, conventions, usage (the card's, or retrievals that rendered its files), an owner, a hand-written or
  learned origin, or a summary, keywords or field somebody edited since the pass wrote the card. Each
  candidate is read again from disk, under the folder's lock, just before it moves. When nothing can move,
  the card is admitted anyway and the notice says so;
- a store already ABOVE the limit moves nothing for a learned or quest card: swapping them one for one would
  exchange the regenerable cards for learned ones for good. The card is admitted and counted as kept above the
  limit, and the report says so. The limit never trims a store it finds above it; only the tidy of provable
  duplicates (see below) or the cleanup script removes anything from it;
- the folder's lock (`.store.lock`, `hubofhubs.recall.card_store_lock`) is shared by every process that changes
  the folder (the lanes, a terminal chat, a tidy), so four writers at the limit still end at the limit and
  every move is counted once. Nothing done while it is held is proportional to the size of the store: the
  count is a listing of file names, remembered until the folder changes; the cold cards that may move are
  chosen before the lock is taken and each is read again under it; the notice (which reads every card) is
  built only after every lock is released. A turn never waits for it: the usage stamp in `assemble()` takes it
  only if it is free and skips the stamp when another process holds it.

**The notice**, once per process, when a pass that hit the limit ends (so it describes the store as it is),
when a write is refused, or at open when the store is at or above the limit. On screen it is two warning
lines that fit an 80 column terminal (the transcript shows the first sentence of a warning, cut to the
window): the four counts on one line, then the tidy first and a higher limit second.

    Store full at 1,000: 812 topic, 150 imported, 30 learned, 8 quest
    Run quest-ai-runner cards tidy --apply, or set QAR_MAX_CARDS higher

A larger store shortens the first line step by step (`Over limit 1,000 (1,430): ...`, then without thousands
separators, then `12k` and `1.4M`, then shorter words): all four counts are always on it, within 72
characters, at any size. The log carries the whole text: the biggest folders, where the report is, the
variable, where a lane reads its environment from, and the way to put a moved card back. A startup callback
gets the same two lines. A lane has no screen, so the first task that starts afterwards carries them as a
status line on its live progress feed, once, saying it is about the context store and not about the task,
with the exact tidy command for this store first ("About the context store, not this task: Store full at
1,000: ... . To bring it under the limit run: quest-ai-runner cards tidy --config <the lane config file>
--apply ... Every card it touches is exported first, and --undo ... restores it. Or set QAR_MAX_CARDS higher
(0 = no limit)."). `--config` names `QAR_CONFIG_FILE` when it is set; a local folder store gets `--cards-dir`.
On the Quest backend and Qdrant a new card learned from a run is still added above the limit (a new topic or
imported card is refused there as everywhere): it cannot be rebuilt from the files, and those stores cannot set
a topic card aside to make room without deleting shared data, so the notice says so and the tidy is what brings
the store back under. The breakdown is also written to
`card_limit_report.md` in the store and printed by `quest-ai-runner cards report [--cards-dir DIR]`: counts
by kind (topic cards by top-level folder, imports by source store, learned, managed, moved out), what the
limit has refused or moved since it was reached, the coldest derived cards, and the ways to change the limit.

**Changing it.** Set `QAR_MAX_CARDS=<number>` where the lane reads its environment, then restart the lane.
A lane reads it from, strongest first: the process environment (the service unit's `Environment=` or
`EnvironmentFile=`), the `.env` files named in `env_files` of its `qar.toml`, and the `[env]` table of that
`qar.toml` (see [a lane is a config file](a-lane-is-a-config-file.md)); a terminal chat reads the shell
environment. An AI that runs the lane can do this on request ("raise the QAR card limit to 2000"): it
needs to know only the variable, that `0` means no limit, and which of those places this lane uses.

**Looking at what moved.** `<store>/_quarantine/<reason>/` holds each moved card as it was (reasons:
`limit`, `compounded`, `duplicate-files`, `item-id`, `superseded-import`, and the ones the cleanup script
adds). Moving the file back into the store puts a card back that was only moved. A card whose notes, tags or
usage were JOINED onto another card first (a protected copy, a stray `card:<id>`) is undone exactly with
`quest-ai-runner cards tidy --undo <manifest>` (see below); moving such a file back by hand would leave what
was joined on both cards.

**Merging** a store into a wider one (`merge-into`) refuses, as a problem in its plan, to leave the wide
store above its limit.

## Tidying a store: at open, and by hand

**The tidy at open.** A store built with `self_heal=True` (the runner's builder does) moves cards that are
provably copies of other cards of the store out to `_quarantine/` before the index is built and at the end of
a pass, paced so the first turn is not starved, and says "Tidied N duplicate context cards. They are kept in
a folder and can be put back." once. The startup tidy costs a clean store one listing of file names: it plans
nothing unless a name shows a vector point id (`card:<id>`) or an id that stacks import prefixes, and a plan
is made from the cards the store has already parsed for its turns, so a large store is not read twice. The
tidy after a pass that wrote cards plans in full. What may never move is one definition,
`card_origin.protection`: a card another system manages, a used card (its `usage_count`, or retrievals that
rendered its files), one with notes or edits, quest tags or conventions, one learned from a run, one with no
provenance (a person wrote it), and one whose summary, keywords or any field changed since the index pass
wrote it (the pass records `provenance.index_hash`; a card without one is judged as before). Of the rest, a compounded copy and an exact duplicate
(the same files and the same name) move; the same files under a different name are two topics of one file,
and both stay. A protected compounded copy, or a card stored under a vector point id (`card:<id>`), moves
only after everything it holds is joined onto the card it stands for: notes by item id (of two items with one
id, the newer by `ts`; two that differ with the same `ts` cannot be told apart), quest tags as a union, usage
and the heat on the files added; a name, description or summary that differs cannot be joined, a card of
another system is never folded, and a stray naming a card of another store is left, so those stay where they
are and are counted ("left in place on purpose"). A join is done once: the card joined onto records the ids
already joined (`provenance.joined_copies`), so a tidy killed between the join and the move does not count the
copy's usage twice when it runs again. Each card is read again, under the folder's lock, just before it moves;
one manifest line per move is appended as it happens (`_quarantine/<time>-<pid>-<random>-tidy-manifest.jsonl`,
one file per process; it names the card that stands for the one moved and what was joined onto it), and only
one process tidies a folder at a time. A failure is logged as a warning with the trace. An edit that comes
for a card after it moved (a run's note) goes to the card that stands for it, a card the limit moved out
comes back with the note, and one that can land nowhere is logged in full at WARNING.

`quest-ai-runner cards tidy [--cards-dir DIR]` does the same by hand with what the package ships: a dry run
that lists what would move and what stays and why, and `--apply` to move it. `--undo MANIFEST` takes a tidy
back: each card returns to the index and what was joined onto the card that stood for it is taken off again.
`cards tidy --merge-topics` also joins a card that pins the same files as another card under a different name onto
that card, instead of keeping both as distinct topics (the default): the kept card gains the other's keywords and
nothing else of it changes (name, summary, files and usage stay), the other card is removed, and the store is smaller
at the cost of less precise topics. It works on a folder (where it looks for that alone) and on the Quest backend or
Qdrant, writes the same export, and `--undo` with the manifest it prints restores both cards.

`scripts/quarantine_cards.py
--cards-dir DIR` (in the source tree) is the same classification plus the judgement calls (excluded folders,
overlap, thin cards), a dry run until `--apply`; its output starts with the store's health: cards by kind, ids
that stack import prefixes, cards pinning the same files, folders of the corpus, and, per reason, how many
cards were created in the last 14 days (a card carries `provenance.created_at` from when it is first
written; older cards have no date and are reported as such).

A corpus folder that is renamed together with the store inside it (`<root>/.quest-context`) records its new
root at the next pass instead of being refused; a store kept elsewhere for a root that still exists is still
refused (use `context move-root`).

## Tidying a store on any backend

The tidy above works on a folder of card files. Most lanes do not have one: anyone with Quest credentials keeps
their cards on the Quest backend by default, and an adopter who cannot reach the machine that runs the backend
still needs an inflated store cleaned up. One lane's backend store held 5,488 cards: about 864 under copy-import
ids, 2,116 about nested repositories the folder review excludes, 1,062 duplicates of one file set. So the runner
tidies through the `CardRepository` port itself (`adapters/card_store_tidy.py`), the same way on a local folder,
the Quest backend and Qdrant.

**What it removes.** The plan is `plan_cards` (`hubofhubs.recall.card_store_tidy_plan`), the rules that were
`scripts/quarantine_cards.py` and that the script now imports: a card stored under a vector point id (its notes
join the card it names), a compounded copy that has a surviving card with the same or more files, a card with
more than half of its files under folders the folder review excludes for good, a surplus card pinning the same
files as another under the same name (the best keeper stays; the same files under another name are two topics
and both stay), and a card about one generated data file. It looks only at the cards the lane OWNS (on a shared
backend, the cards its writer origin wrote). It never removes a card another system manages, a used card, one
with notes, quest tags or conventions, one learned from a run or written by a person, except a provable copy
whose holdings join its keeper first.

**What it changes.** For each card that stands for others, the tidy reads it again and skips it when its revision
changed since the plan (other writers exist); it joins the keywords of every card it removes onto the keeper (a
union, the keeper's own order first; never its name, summary, files or usage, apart from what a provable copy
holds), writes the keeper once, then reads each removed card again and removes it: moved into
`_quarantine/<reason>/` on a local folder, deleted on the backend. It works in paced batches, stops at the
first write or removal that fails, and ends by listing the store again to check every removal and keeper. A
second run finds nothing.

**The export.** A backend has no quarantine folder, so BEFORE the first write the tidy writes every card it will
remove and every keeper it will update, as they are, to `<cards dir>/tidy-state/<run>-tidy-export.json`, with the
store's identity (repository kind, backend address, team or org, user; never a key), and a manifest beside it,
`<run>-store-tidy-manifest.jsonl`, that gains one line per change as it happens. The folder is mode 700 and the
files 600; `<cards dir>` is the lane's local card folder (`context_cards_dir`, or `<corpus>/.quest-context`, which
a consumer keeps out of git), the same place the backend listing cache lives.

**When it runs.** In the background thread that warms the store at startup (no turn waits on it), when the
store's own cards are over its card limit (every reason above) or, on a repository other than a local folder,
when ids prove some cards copies (the vector point id and compounded rules only; a local folder's provable copies
are the tidy at open above). What it then does is the lane's `card_tidy` setting, in `qar.toml` like any other
`RunnerConfig` field:

```toml
card_tidy = "apply"   # or "plan", or "off"
```

Unset, a local folder applies (a move into `_quarantine`, undone by moving it back) and the Quest backend and
Qdrant PLAN: their cards are shared team data, and deleting them is the owner's call.

**A second step to stay under the limit.** When the store would still be over its card limit after the plan, the
plan gains a clearly labelled second step: the `--merge-topics` join (a card that pins the same files as its
keeper under another name is joined onto that keeper; its keywords join, nothing else of the keeper changes).
Both steps are one plan, one export and one manifest (the second step's lines carry `"step": "merge-topics"`), so
one `--undo` takes both back. With `card_tidy = "plan"` (the backend default) it only says that the second step
would bring the store to N cards; with `"apply"` it carries both out.

**Where it is said.** A plan is written to `<cards dir>/tidy-state/last-plan.json` (the second step under
`second_step`, with the count it would leave) and said once, in the log, to a startup callback and, because a
lane has no screen, as a status line on the next task's live progress feed (the same channel as the card limit's
notice): "The context store needs a tidy because it holds 5,488 cards, over its limit of 1,000: removing N copies
and cards about excluded folders (...) would leave M cards, and a second step, joining K cards that pin the same
files under another name onto their keeper, would bring the store to J cards; nothing was changed. To apply both
steps: quest-ai-runner cards tidy --config <the lane config file> --apply --merge-topics (the first step alone:
... --apply). To tidy automatically, set card_tidy = "apply" in the lane config. Every card it touches is exported
first, and --undo with the manifest it prints restores it." The same plan is not said again at the next start.
Applied, the same places get "Tidied N context cards ...; undo with: quest-ai-runner cards tidy --undo
<manifest> ...".

**The final check.** After the changes the tidy lists the store again: every removed card must be gone and
every keeper must hold the keywords it joined. A card the listing still shows is read again on its own for up to
two seconds before the check counts as failed (a backend may list a delete late). Each failed check is saved in
the manifest's last line (`summary.checks`: the card, the check and why it stays, with `failed_checks` the count),
`cards tidy --apply` prints the count, and the lane's log names them. Manifest names sort in the order the runs
started (to the nanosecond, then a counter), so `latest_manifest` is the last run even within one second.

**For an adopter with only Quest credentials.** Nothing to install or reach: run the lane as usual. On its first
start over the limit it writes the plan and prints the line above. Read the plan, then either run
`quest-ai-runner cards tidy --config <your qar.toml>` (a dry run that lists what would go and what stays and why)
and `quest-ai-runner cards tidy --apply --config <your qar.toml>`, or set `card_tidy = "apply"` and restart the
lane. `--config` (or `QAR_CONFIG_FILE`) puts the lane's environment in place, so the command reaches the store the
lane uses.

**Undo.** `quest-ai-runner cards tidy --undo <cards dir>/tidy-state/<run>-store-tidy-manifest.jsonl --config <your
qar.toml>` re-creates every card the run removed and restores every keeper it updated, through the same port,
newest change first. It refuses a manifest written for another store. A keeper changed since the tidy is left as
it is and counted. A card stored under a vector point id cannot be re-created on any repository (every writer
refuses that id); it stays in the export. On the backend a re-created card gets the backend's own fields
(revision, update time) again.

## A person who belongs to an org and also runs a personal lane

The personal lane's corpus root sits above the org's folder, so the org's store is nested inside it.

- If the personal lane's OS user can read the org store's directory, the personal lane mounts it
  read-only. It indexes nothing under that folder and holds no copy; when the org's lanes update their
  cards the personal lane reads the update.
- Nothing the personal lane produces is written to the org store or to a space the org can read. Its
  cards, turns, rep prefs, locks and stamps are under its own `cards_dir`. Its embeddings and task
  associations carry the personal store's `store_id`, and the org's lanes only ever query their own
  store and the stores they mount, so they cannot retrieve them even on a shared Qdrant server.
- The org's lanes never mount the personal store unless it sits within their corpus and their OS user
  can read it. A personal store outside the org's tree is not discovered at all; one inside it that
  the org's user cannot read is skipped and logged.

## The Quest backend (default)

### Selection

| Setting | Meaning |
|---|---|
| `QAR_QUEST_API_URL`, else `QUEST_BASE_URL` | backend base URL |
| `QAR_QUEST_API_KEY`, else `QUEST_API_KEY` | Bearer key; its user is the acting user |
| `QAR_TEAM_ID`, else `QUEST_TEAM_ID` | the team a lane writes cards to; unset means private cards |
| `QAR_RUNNER_ENV_ID`, else `QAR_ENV_ID`, else hostname | this runner environment's id |
| `QAR_CARDS_SCOPE` | the scope this lane writes cards to: `org:<id>`, `team:<id>`, `private`, or bare `org`. Unset keeps the default (the lane's team, private when it has none). An id inside the value wins over the variables below |
| `QAR_ORG_ID`, else `QUEST_ORG_ID` | the org for a bare `QAR_CARDS_SCOPE=org`. When it is not set the org is resolved from the lane's team (`GET /api/teams/{team_id}`, field `org_id`) |
| `QAR_USER_ID` | optional hint only; the key's identity (`GET /api/teams/whoami`) always wins and a difference is logged as a warning |
| `QAR_CARDS_BACKEND` | unset or `auto`: Quest when credentials exist, otherwise local. `local` (alias `file`): the opt-out. `qdrant`: one Qdrant collection (unchanged). `quest`: require Quest; without credentials it logs an ERROR, notifies, and runs local, so the ERROR is the flag that cards are not on the backend |

An unusable `QAR_CARDS_SCOPE` (unknown form, `org` with no org and no team that belongs to one, a failed
team lookup) stops startup with `CardScopeError`; a lane never quietly runs on a different scope. For an
org scope the lane's team stays only as provenance on the `local_fetch` item; writes go to the org.

The `QAR_*` name wins when both names are set. Only the environment is read (a lane's `qar.toml`
`[env]` table is the usual source); `RunnerConfig.quest_base_url` is not consulted for cards.

Under the Quest backend a lane does NOT embed or index cards: the auto-built local vector store and its
seeding are skipped, and the vector arm is `QuestApiCardVectorStore`, which calls the backend's search
and hands its ranking to the assembler. A `cfg.vector_store` a consumer sets explicitly is left alone.
Mounting is off (cards are remote; nothing to mount), logged at info. Cards a lane writes carry its
`team_id`, `runner_env_id` and the `local_fetch` preview.

### Access model (enforced by the backend)

- A card has an owner user and exactly one scope: private, a team, or an org.
- Readable by its owner and by members of its team or org. Writable and deletable by the owner, or by a
  member writing the team's or org's card. An org card is readable by every member of that org.
- Listing, search and assembly default to everything the key's user can read: their private cards plus
  the cards of every team they belong to and of every org they belong to (the backend defines org
  membership; see its `docs/CONTEXT_CARDS_API.md`). A person's personal lane therefore reaches an org team's
  cards only through that person's own team membership. `read_team_ids` / `read_org_ids` narrow a
  repository to chosen teams / orgs (sent as `team_ids` / `org_ids`).
- Card identity is `(scope, card_id)`, scope being the owner (private), the team or the org. The same id
  may exist in several scopes. The repository presents a `{card_id: card}` mapping, so on an id
  collision the card in the lane's own write scope wins, then the private card; updating a card writes
  it back to the scope it was listed from, never into a different one.
- A service account (a key whose user has no interactive identity) may act only for users it shares a
  team with, and only on those teams' cards.
- Team and `runner_env_id` on a card are provenance as well as scope; the access decision is the key's
  team membership.

### Writer origin: a lane only mutates the cards it wrote

The listing a lane reads on the Quest backend is every card its key can read, which includes other
lanes' cards and other team members' cards, written against THEIR corpus roots. Reading and assembly
use all of them. Anything that REWRITES or DELETES cards does not.

- Every card a store writes carries an `origin` field (existing fields are kept): the store identity's
  `store_id` when the lane has a local `cards_dir` identity, else a deterministic id derived from
  (runner env id, resolved corpus root), `env_<hash>`. Turn cards, conversation cards, file-derived
  cards, `write_card`, `record` and `update_card` all stamp it. A card that already has an origin
  keeps it (a usage-stat write on a teammate's card never takes ownership of it).
- `FileContextStore._own_cards()` is the single gate. With an injected repository it returns only the
  cards whose `origin` is this store's (its `store_id` or its derived id; a store that gains an
  identity after it wrote cards keeps those). Every mutating path works from it: the bootstrap/refresh
  pass (dead-card prune, imported-copy prune, summary heal, stale detection and regeneration, tfdfidf
  migration, dedup/folding into existing cards, card ceilings), `prune_only` (keyword cap, summary
  heal, dead and degenerate removal), `find_similar_card` (a near-duplicate is merged INTO, so only an
  own card qualifies) and the "store is empty, bootstrap" check. A new card whose id is already taken
  by another origin's card is written under a suffixed id; it never replaces it.
- Usage-stat writes (`mark_sources_used`) may touch any readable card because they change no content
  and never change its origin.
- Legacy cards with NO `origin`: in local file mode (no injected repository) every card is own, as
  before. With an injected repository such a card is own only if its `runner_env_id` equals this
  runner's AND it pins at least one file AND every pinned path is a relative path that resolves to an
  existing file under this lane's root; otherwise it is read-only here.
- Legacy cards are corrected once, not judged forever: every refresh stamps this store's origin on
  each own card that has none (`_backfill_origins`), so after one local refresh (or a `context
  upload`, which stamps too) a store has no origin-less cards left and a later move to a shared
  backend needs no second backfill. `origin` is not part of the embedded text, so the stamp never
  re-embeds. On a shared repository only provably own legacy cards are stamped; a fully dead legacy
  card there (its files gone) is not provably ours and is left for its owner or for removal by hand.
- `quest-ai-runner context upload` stamps the uploading store's origin on every card it sends, so the
  lane that owns that store manages them afterwards.
- Residual: two lanes with the same env id AND the same resolved root are one origin by definition.

### Protocol the repository speaks

`PUT/GET/DELETE /api/cards/{id}[?team_id= | ?org_id=]` (never both), `GET /api/cards` (paged with `limit`/`cursor`, delta with
`updated_after`), `GET /api/cards/stamp` (count, newest `updated_at`), `GET /api/cards/search`,
`GET /api/cards/embedder`, `POST /api/cards/bulk` (at most 100 cards per request, optional vectors).
Server-owned fields (`updated_at`, `revision`, `owner_user_id`, `user_id`, `card_id`) are never sent
on write. `revision()` is the stamp, so an unchanged store costs one small request per check; a
change is fetched as a delta, and one full refresh follows only if the count shows a deletion.

### Failures are loud

A backend or transport error is logged at ERROR with the method, path, status and the backend's
reason, counted (`error_count`), kept (`last_error`), passed to the lane's notify callback, and exposed
by `health()`. While degraded, every assembly adds a `CARD BACKEND ERROR` source to the run's context
sources, which the orchestrator emits as a status event. A failed listing serves the last good listing
(or none if there never was one) and never reads as a silent "no cards"; a missing card (404) is not
an error. If whoami fails the repository is still the Quest backend (a lane does not quietly split
into local cards), the error is logged, and the identity is retried on the next call.

### Offline cache

`CardCache` (`adapters/quest_api_card_repository.py`) is the interface for an offline listing cache and
write outbox (`load_listing`, `store_listing`, `enqueue_write`, `pending_writes`). The default is
`FileCardCache` (`adapters/card_cache_file.py`), chosen through the `cache-cards` row of the selection
table (`RunnerConfig.selections`, `null` for none): two atomic JSON files per lane under
`<cards_dir>/.api-card-cache/<lane>/`. When the backend cannot be reached on a cold start, the repository
serves the stored listing (the error stays loud and counted) and does a full refresh on the next
successful call. Failed writes are queued, oldest first; nothing replays them yet.

### The card limit and the tidy

The [card limit](#the-card-limit) applies here too: it counts the cards this lane wrote, gives the same notice
and breakdown (`quest-ai-runner cards report --config <qar.toml>` prints it from the backend), and a full store
adds no derived card; it never trims the store. The [tidy through the repository](#tidying-a-store-on-any-backend)
does, after a local export, and on this backend it only PLANS until the lane sets `card_tidy = "apply"`.

### Opting out

`QAR_CARDS_BACKEND=local`. Cards are per-card JSON under `cards_dir`; mounts, `merge-into`, `move-root`,
remote-vector readers and the local vector arm behave exactly as documented above.

## Uploading a local store (`context upload`)

```
quest-ai-runner context upload --cards-dir DIR (--team-id ID | --org-id ID | --private) [--apply]
        [--adopt-ancestor-copies] [--qdrant-url URL | --qdrant-path PATH] [--collection-prefix PREFIX] [--vector-size N]
        [--embedder-id ID] [--base-url URL] [--api-key KEY]
```

Dry run by default: it prints how many cards would upload, in how many batches of 100, what is
skipped and why, and how many vectors are usable, and sends nothing. `--apply` uploads and prints the
backend's per-batch counts; it exits non-zero if any card failed. Re-running upserts the same ids.

- Exactly one scope is required: `--team-id` assigns every card to that team, `--org-id` to that org
  (readable by every org member), `--private` leaves them private to the key's user.
- `--adopt-ancestor-copies`: copy-imports whose `provenance.imported_from` starts with `..` come from
  a store above this root, often a stale ad-hoc one that will never be uploaded, and their file paths
  are already relative to this root. With the flag they upload as this store's own cards: `imported_from`
  is dropped, `adopted_from` records the old value, the id and paths are kept, this store's origin is
  stamped and local vectors are used if present (plan line "adopted from an ancestor store: N"). Nested
  imports (a store below this root, which has its own lane) stay skipped. Because the uploaded copy
  carries this store's origin, the store's own refresh treats it as its own afterwards.
- Skipped: namespaced copy-imports (provenance `imported_from`; "the source store uploads them") and
  mounted stores (read-only views that are not files in this store).
- Vectors: with `--qdrant-url` or `--qdrant-path` the store's existing card vectors are read
  (read-only) and sent with the store's embedder id and dimension. The backend accepts a vector only
  when both equal its own embedder (`GET /api/cards/embedder`); the command also checks this first and
  withholds vectors that cannot match, so the backend embeds those cards itself. The embedder id is
  `--embedder-id`, else the store's recorded `vector_location` embedder, else `QAR_EMBEDDER_BACKEND`.
  Nothing is ever embedded locally by this command.
- A card is sent as stored (minus server-owned fields and any stale `team_id`) with `runner_env_id` and the
  uploading store's `origin`;
  it does not get the `local_fetch` preview, because the uploaded store may no longer be served by a
  lane.

## Measured guarantees

Asserted in `tests/test_card_store_mounts.py`:

| Guarantee | Test |
|---|---|
| Re-embeds on a root move = 0 | `TestVectorMounting::test_move_root_re_embeds_nothing` |
| Points per distinct card across all mounting corpora = 1 | `test_a_card_is_embedded_once_however_many_corpora_mount_it` |
| Cards copied between stores in mount mode = 0 | `TestMountRead::test_nested_store_is_read_in_place_with_prefix_and_zero_copies` |
| An unreadable or uninitialised store is not mounted and not indexed by the reader | `TestReaderDoesNotIndexMountedSubtree` |
| A reading lane never writes into a mounted store (cards, usage stamps, vectors) | `TestWriteIsolation`, `test_a_reader_cannot_write_vector_items_for_a_mounted_store` |
| An owner change is visible to the reader with zero reader writes | `TestFreshness` |
| The vector arm returns only points of the stores actually mounted | `test_reader_cannot_get_points_of_a_store_it_did_not_mount`, `test_task_associations_are_attributed_to_their_lane_and_not_served_to_others` |

Asserted in `tests/test_card_store_merge_and_remote.py`:

| Guarantee | Test |
|---|---|
| Re-embeds on a merge, legacy or qualified scheme = 0 (the embedder is called 0 times) | `TestMergeVectors::test_legacy_scheme_points_keep_their_ids_and_only_the_label_changes`, `test_qualified_scheme_points_are_copied_with_their_stored_vector_then_the_old_ones_go` |
| The wide store's next seed pass after a merge re-embeds 0 cards | same two tests |
| Points per card after a merge = 1 (old points removed with `--prune-source`) | `test_qualified_scheme_points_are_copied_...` |
| Merge dry run writes nothing; the source is untouched without `--prune-source` | `TestMergeCards::test_dry_run_reports_counts_and_writes_nothing`, `test_apply_moves_cards_with_ids_unchanged_and_paths_prefixed` |
| Id collisions overwrite nothing; a re-run changes nothing (cards, points, meta) | `test_id_collision_keeps_the_wide_card_renames_the_incoming_and_rerun_is_a_noop`, `test_rerun_with_vectors_is_idempotent` |
| A merged store is not a store to any discovery path | `test_prune_source_removes_moved_cards_marks_it_merged_and_discovery_ignores_it`, `test_copy_mode_discovery_also_treats_a_merged_store_as_not_a_store` |
| Moved cards are fresh at the wide store with 0 re-cards | `TestMergeFreshness` |
| Writes by a reader to a mounted store's other-backend location = 0 | `TestRemoteMountVectors::test_reader_queries_the_mounted_points_on_the_other_backend_and_writes_nothing`, `test_the_readers_own_writes_never_reach_the_mounted_location` |
| Cards embedded locally for a remote mount = 0 (only the query is embedded) | the first of those |
| Incompatible embedder, wrong dimension, unreachable server, embedded elsewhere: keyword arm still works | the remaining `TestRemoteMountVectors` tests |
| Default behaviour (copy mode, no recorded location) unchanged | `TestDefaultsUnchanged`, `test_a_mount_with_no_recorded_location_or_the_same_backend_is_unchanged` |

## Migrating a deployment

This section is for the LOCAL backend (`QAR_CARDS_BACKEND=local`). To move a deployment onto the Quest
backend, upload each owner store with `context upload` (owners before readers; skip copies, since
the source store uploads the original), then remove the opt-out. Nothing about a local store changes
until a lane sets `QAR_CARD_MOUNT=mount`. To migrate, owners first, readers after, one
lane at a time:

1. **Give every owner store an identity.** For each `.quest-context` that other corpora might mount
   (and for each reading lane's own store): `context store-info --cards-dir DIR --init` (dry run),
   then with `--apply`. A store that completes a bootstrap with this version also gets one.
   Stores that already hold embedded points are marked `legacy`, so their point ids do not change.
2. **Stamp the owners' existing vector points with their store id.** Restart the owner lanes (or let
   their next seed pass run): `sync` writes `store_id` onto existing points without re-embedding. Do
   this before step 4, because a fenced reader does not serve unattributed card points.
3. **Optional:** declare the vector space on owners and readers that share a server:
   `context store-info --cards-dir DIR --vector-space NAME --apply` and `QAR_VECTOR_SPACE=NAME`.
   Owners whose points are on a server a reader with a PRIVATE vector store must query: publish the
   location instead (`--vector-location URL --apply`), see
   [A private reader of a shared vector server](#a-private-reader-of-a-shared-vector-server).
4. **Convert each reading lane.** For a lane whose store holds copies:
   `context migrate-imports --cards-dir DIR` (review), then `--apply`, adding
   `--qdrant-url`/`--qdrant-path` and `--prune-points` to delete the orphan points. Then set
   `QAR_CARD_MOUNT=mount` in that lane's config and restart it.
5. **Order across lanes:** owners (steps 1-2) before any reader; within the readers, the narrowest
   corpora first (their mounts are leaves), then wider ones. A reader whose nested store has no
   identity yet mounts nothing from it and indexes nothing under it, so do not skip step 1.
6. **To widen a corpus root** (a lane moves from `X/sub` to `X`): `context merge-into`, see
   [Widening a corpus root](#widening-a-corpus-root-merge-into).
7. **To move a store's root:** `context move-root --cards-dir DIR --new-root PATH` (review the missing
   file count), then `--apply`, then point the lane's corpus root at the new path. Readers follow
   without any change of their own.

To roll back a lane, set `QAR_CARD_MOUNT=copy` and run a bootstrap; the copy-import resumes. Mount
records in `bootstrap_meta.json` are ignored in `copy` mode.

## Command line

- `quest-ai-runner paste-context "<text>"` saves text (or stdin) as a context card; `--card-id` names it,
  `--goal-id` ties it to a goal.
- `quest-ai-runner search-context "<query>"` shows which cards a query selects; `--no-llm` skips the model
  and ranks by keywords only.
- `quest-ai-runner cards report [--cards-dir DIR]` prints what fills a store, what its card limit has refused
  or moved, and how to change the limit (read only; `--write` also rewrites `card_limit_report.md` in the store).
- `quest-ai-runner cards tidy [--config FILE] [--cards-dir DIR] [--apply | --undo MANIFEST]` lists, or with `--apply` moves, the
  cards that are provably copies; a card with notes, usage, quest tags or a hand-written origin is never moved.
  `--undo` takes a tidy back, including what it joined onto the cards that stood for the ones it moved.
- `quest-ai-runner reconcile-plan <quest_id>` compares a quest's plan with what happened and prints the
  changes it would make; `--write` applies them.
