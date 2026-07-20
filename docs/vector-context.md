# Vector context layer — semantic orientation for every run

> Status: design-of-record for the vector search capability added alongside
> the existing keyword/IDF ``FileContextStore``.  Implemented behind the
> ``VectorStore`` Protocol (defined in ``core.adapters``) so the core stays
> dependency-free.  Heavy deps (qdrant-client, fastembed) live behind the
> optional ``[qdrant]`` extra.

---

## Why a vector layer?

The keyword/IDF ``FileContextStore`` is excellent at routing tasks to the
**exact files** they mention (symbol names, path segments, identifiers).  It
misses semantics: a task phrased as "clean up the payment pipeline" may not
contain the token ``billing`` even if ``billing/`` is exactly where the work
lives.

Vector search closes that gap.

---

## Three arms: sparse-content + dense-summaries + keyword-IDF

The library ships three complementary retrieval arms.  Use them together for
the highest recall:

| Arm | Indexes | Finds | Misses |
|-----|---------|-------|--------|
| **``FileContextStore``** (stdlib) | Card keyword summaries + symbol names (IDF) | Files whose path segments or symbols match the task | Exact phrases in file bodies; semantic paraphrase |
| **``VectorContextAssembler``** (``[qdrant]``) | Summaries / topics as dense vectors | Semantic / paraphrase matches | **Full file content is NOT embedded** -- rare identifiers, exact strings |
| **``BM25ContentStore``** (``[bm25]``) | **Actual file content** -- every token in every file | **Exact identifiers, rare tokens, specific phrases** in un-embedded content | Pure semantic paraphrase |

**The key insight:** the dense vector arm embeds *summaries and topics*, not
full file bodies.  A distinctive identifier like ``XFCALLBACK_7Q2`` or a
legacy constant that never appears in any summary is invisible to the vector
index.  ``BM25ContentStore`` (sparse BM25 over actual content) fills that gap
and is the correct first responder for "find every file that uses identifier X."

### Sparse BM25 over content -- parallel multi-query

``BM25ContentStore`` walks the corpus root, reads each file's actual text, and
builds a BM25 index over the content.  When a ``ModelProvider`` is wired, it
generates diverse keyword queries IN PARALLEL for higher recall, then fuses
hits across all queries (best score per file wins).

```bash
pip install 'quest-ai-runner[bm25]'
```

```python
from quest_ai_runner.adapters.bm25_content_store import BM25ContentStore

store = BM25ContentStore(root=".", confidence_threshold=0.0)
ac = store.assemble("XFCALLBACK_7Q2")
# ac.card_ids contains the files that mention that exact identifier
```

The two vector/sparse modes are **complementary**: use ``HybridContextAssembler``
(or a custom composite) to run all arms in parallel and fuse their results.

---

## Local-filesystem Qdrant (the default)

``QdrantVectorStore`` defaults to an **embedded** Qdrant instance whose
state lives on the local filesystem.  No server is needed; no Docker, no
cloud account.

```python
from quest_ai_runner.adapters.qdrant_vector_store import QdrantVectorStore

# Default: state under <cwd>/.quest-context/qdrant
store = QdrantVectorStore()

# Or specify a path
store = QdrantVectorStore(path="/data/my-project/.quest-context/qdrant")
```

The default embedder is ``fastembed`` (``BAAI/bge-small-en-v1.5``, 384-d).
It runs locally via ONNX — no API key, no network call at embed time.

Install the extra:

```bash
pip install 'quest-ai-runner[qdrant]'
```

### Turning the auto-built vector arm on/off (``QAR_VECTOR_BACKEND``)

When no explicit vector store is configured, ``resolve_context_assembler`` auto-builds
this Qdrant store so hybrid search is on by default.  The ``QAR_VECTOR_BACKEND`` env
var controls that attempt:

| Value | Behavior |
|-------|----------|
| ``auto`` (default / unset) | Attempt Qdrant; fall back to keyword-only search with a warning when it cannot be opened |
| ``none`` / ``off`` | Skip the Qdrant attempt entirely: keyword-only search, no warning logged |
| ``qdrant`` | Require Qdrant: log an error when it is unavailable (still degrades to keyword-only so the runner starts) |

Set ``none`` on deployments that intentionally run without the ``[qdrant]`` extra so
startup does not log a "Qdrant open failed" warning on every build.  The variable never
overrides an explicitly configured ``vector_store`` or the qdrant card backend's
query-only vector arm.

---

## Remote / self-hosted Qdrant

Pass a ``url`` to connect to any Qdrant deployment (cloud, self-hosted):

```python
store = QdrantVectorStore(url="http://localhost:6333")
# or
store = QdrantVectorStore(url="https://my-cluster.qdrant.io", ...)
```

---

## Custom embedder (BYO)

Any callable ``(texts: List[str]) -> List[List[float]]`` works:

```python
def my_embedder(texts):
    # call your own model / API
    return [[0.1, 0.2, ...]] * len(texts)

store = QdrantVectorStore(embedder=my_embedder, vector_size=1536)
```

---

## Custom VectorStore (fully pluggable)

You do not have to use Qdrant at all.  Any class that satisfies the
``VectorStore`` Protocol (defined in ``quest_ai_runner.core.adapters``)
works as a drop-in:

```python
from quest_ai_runner.core.adapters import VectorStoreBase, VectorHit

class MyStore(VectorStoreBase):
    def search(self, query, *, scope=None, top_k=8):
        ...  # return List[VectorHit]; never raise
    def upsert(self, items, *, scope=None):
        ...  # embed + store; never raise
    def sync(self, items, *, scope=None):
        ...  # auto-update; return count; never raise
```

Wire it into ``VectorContextAssembler`` the same way as ``QdrantVectorStore``.

---

## Task-to-context associations (record enrichment)

``VectorContextAssembler.record(task_text, outcome)`` builds a **task-to-context
association** and upserts it into the vector store.  This is the compounding loop:
each completed task contributes a searchable mapping from "the kind of task" to
"which code region it touched", so future similar tasks retrieve the right region
immediately.

### What is embedded

The embedded text is: ``task_text`` + a structural description of the region used
(file paths and symbol names from ``outcome``).  If a ``ModelProvider`` (``provider``)
is wired, one cheap LLM call generates a one-line orientation summary instead (best-
effort; falls back to structural description on any failure).

**Corpus content is never embedded.**  The vector arm embeds only summaries and
task-to-context associations.  BM25ContentStore covers exact-content search.

### Payload shape

The upserted payload is rich so a top hit gives the consuming agent directly useful
metadata:

```json
{
  "task":    "implement the billing collator",
  "paths":   ["billing/collate.py", "billing/models.py"],
  "symbols": ["PaymentCollector", "xfr_collate_payments_7q2"],
  "summary": "billing collator touched billing/collate.py ...",
  "kind":    "met"
}
```

When a vector search retrieves this association the rendered context view shows:
``matched task``, ``summary``, ``read these files``, and ``symbols`` so the agent
knows exactly where to look without a grep pass.

## AUTO-UPDATE: sync re-embeds only changed items

The ``sync(items)`` method is the zero-management auto-update entry point.
It:

1. Fetches the stored ``fingerprint`` for each item id.
2. Compares it to the fingerprint in the incoming ``items``.
3. Re-embeds and upserts **only** items whose fingerprint changed or are
   missing from the index.
4. Returns the count of items re-embedded.

Unchanged items are not touched.  Callers call ``sync`` on every run
(e.g. with a list of card summaries + their sha256 fingerprints from the
repo) and the index stays fresh with no manual re-index step.

```python
store.sync([
    {"id": "billing/collate.py", "text": "def xfr_collate...", "fingerprint": "abc123"},
    {"id": "core/orchestrator.py", "text": "class Orchestrator...", "fingerprint": "def456"},
])
# Returns 0 if both fingerprints match what is already stored.
# Returns 1 if only one changed.
```

---

## Agentic retrieval flow

``VectorContextAssembler`` implements a four-step agentic retrieval when a
``ModelProvider`` is wired:

```
Task text
   │
   ├─► [LLM] Generate N diverse queries (parallel queries for better recall)
   │
   ▼
All queries (raw task + LLM-generated)
   │
   ├─► vector-search each query IN PARALLEL (ThreadPoolExecutor)
   │      ↓ deduplicate hits by id, keep best score
   ▼
Candidate hits
   │
   ├─► [LLM] Review: select only hits genuinely relevant to the task
   │
   ▼
Reviewed hits → confidence gate (score >= confidence_min_score)
   │
   ▼
context_view (rendered hits) → AssembledContext → Orchestrator
```

When no provider is given the LLM steps are skipped: only the raw task text
is searched and all hits above the confidence gate are kept.

### Query generation

The LLM is asked for ``num_queries`` (default 3) short, diverse queries.
These are issued alongside the raw task text, so a single vector search
call becomes ``num_queries + 1`` parallel searches.  All results are
deduplicated by hit id, keeping the best score from any query.

### LLM review

After deduplication the LLM is shown the candidate hits and asked to select
only those genuinely relevant to the task.  Irrelevant results are filtered
out before the confidence gate.  If the review call fails, all candidates
are kept (best-effort).

### Confidence gate

Only hits with ``score >= confidence_min_score`` survive into the context
view.  When nothing qualifies the returned ``AssembledContext`` is empty and
the caller falls back to plain Claude Code (the never-worse guarantee).

---

## Hybrid keyword + vector fusion (recommended)

``HybridContextAssembler`` runs both assemblers IN PARALLEL and fuses their
results:

```python
from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.adapters.vector_context_assembler import VectorContextAssembler
from quest_ai_runner.adapters.hybrid_context_assembler import HybridContextAssembler
from quest_ai_runner.adapters.qdrant_vector_store import QdrantVectorStore

keyword_asm = FileContextStore(".quest-context/cards", repo_root=".")
vector_asm  = VectorContextAssembler(QdrantVectorStore())
hybrid      = HybridContextAssembler(keyword=keyword_asm, vector=vector_asm)

# Wire into RunnerConfig:
config = RunnerConfig(..., context_assembler=hybrid)
```

The fused ``context_view`` contains clearly labelled sections:

```
## Keyword context (IDF cards)

### Card: billing-collate-...
billing/collate.py: xfr_collate_payments_7q2, PaymentCollector
...

---

## Vector context (semantic hits)

### Vector hit: billing/collate.py  (score=0.843)
  path: billing/collate.py
  text: def xfr_collate_payments_7q2(account_id): ...
```

``card_ids`` and ``stale`` are the union of both assemblers' outputs.  If
both return empty the hybrid returns empty (caller falls back to baseline).

---

## Multi-tenant scoping

Every operation accepts an optional ``scope`` dict, e.g.:

```python
scope = {"org_id": "acme", "team_id": "platform", "quest_id": "q-42"}
store.search("payment pipeline", scope=scope)
store.upsert(items, scope=scope)
store.sync(items, scope=scope)
```

All points of a store live in ONE Qdrant collection,
``{collection_prefix}_default_{vector_size}`` — keyed on the embedder's true
dimension (adopted from real embedder output, not just the declared
``vector_size``) so two stores with different embedder configurations sharing
one server never collide on a collection.  Mixed vector sizes in a single
collection make Qdrant decline the mismatched writes point-by-point, which the
never-raises contract would otherwise hide.  A bare legacy
``{collection_prefix}_default`` collection is reused as-is when its configured
size matches the embedder, so pre-existing data needs no migration.
The store hashes the sorted scope items to a short digest stored on each point
as the ``_scope`` payload field, and searches filter on it — the payload-filter
multitenancy model Qdrant recommends over collection-per-tenant.

Visibility rules:

- Unscoped points (``scope=None``) are **shared**: visible to unscoped searches
  and to every scoped search (shared corpus + scope-private additions).
- Scoped points are visible only to searches carrying the **same** scope.
- Unscoped searches see only shared points, never any scope's private data.

Read operations (``search`` / ``count`` / ``evict_oldest``) never create a
collection; only writes do.  (An earlier version created one collection per
unique scope — including on mere search — which sprawled into hundreds of
permanently empty collections on a long-lived shared server, each adding
startup shard-recovery time.  Deployments that ran that version can call
``store.prune_scope_collections()`` once to delete the empty leftovers.)

When used via ``VectorContextAssembler``, the ``meta`` dict passed to
``assemble(task_text, meta=meta)`` is forwarded directly as the scope.
Because shared points are visible under every scope, a caller passing
high-cardinality meta (e.g. a per-goal id) still retrieves the shared corpus
seeded by the keyword bootstrap.

---

## Matching quest-backend (production Quest path)

quest-backend stores embeddings produced by Voyage AI with
``input_type="document"`` in a 1024-d Qdrant collection (model defaults to
``voyage-3-lite`` from the ``VOYAGE_MODEL`` env var).  Voyage distinguishes
document embeddings (for stored items) from query embeddings (for search
queries); both land in the same 1024-d space.

To share the same Qdrant collection and achieve cross-compatible similarity
scores, wire ``QdrantVectorStore`` with **two separate Voyage embedders** — one
per role:

```python
from quest_ai_runner.adapters.qdrant_vector_store import (
    QdrantVectorStore,
    make_voyage_embedder,
)

store = QdrantVectorStore(
    url="http://localhost:6333",          # point at the backend's Qdrant
    vector_size=1024,                     # must match the backend collection
    embedder=make_voyage_embedder(input_type="document"),   # for upsert/sync
    query_embedder=make_voyage_embedder(input_type="query"),  # for search
)
```

``make_voyage_embedder`` reads ``VOYAGE_MODEL`` from the environment (default
``"voyage-3-lite"``).  Install the dependency first:

```bash
pip install voyageai
```

**Embedder quality dial:**

| Embedder | Dimensions | BEIR NDCG@10 | MS MARCO MRR@10 | Notes |
|----------|-----------|-------------|-----------------|-------|
| ``BAAI/bge-small-en-v1.5`` (fastembed default) | 384 | ~53 | ~80 | Zero-config, local ONNX |
| ``BAAI/bge-base-en-v1.5`` (fastembed) | 768 | ~60 | ~86 | Larger local model |
| Voyage AI / SOTA models | 1024 | Higher | Higher | Requires API key |

The local ``bge-small`` / ``bge-base`` fastembed models are the **zero-config
option** (no API key, no network at embed time).  Switch to Voyage for
production-quality recall that matches quest-backend's embedding space.

**Important:** the ``vector_size`` parameter must match the dimensionality
produced by the embedder AND the dimension of the existing Qdrant collection.
Mixing dimensions silently produces wrong similarity scores.

---

## Quality notes

Vector quality depends on the embedder.  The default ``fastembed`` model
(``BAAI/bge-small-en-v1.5``, 384-d) is a lightweight general-purpose English
model.  For codebases with heavy domain-specific terminology, a code-tuned
model (e.g. ``jinaai/jina-embeddings-v2-base-code``) may give better recall
at the cost of a larger model download.

The vector layer is a **semantic orientation** layer, not a precision
retrieval tool.  It surfaces candidates; the LLM review step and the
confidence gate prevent low-quality hits from reaching the context.  Pair it
with keyword/IDF via ``HybridContextAssembler`` for the best coverage.
