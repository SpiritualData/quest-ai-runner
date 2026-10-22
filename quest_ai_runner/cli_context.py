"""Context-card commands: card selection, `context`, `bootstrap`, `paste-context` and `search-context`.

Split out of ``cli.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path

from .pricing import estimate_bootstrap_cost
from .cli_config import _config_from_env, _model_provider_from_env


def select_card_ids_for_text(text: str, *, corpus: Optional[str] = None,
                             cards_dir: Optional[str] = None,
                             use_llm: bool = False, read_only: bool = False):
    """Resolve which existing context cards ``text`` selects, via the same card store the
    ``search-context`` command and card-aware task creation (``send``) both use.

    ``corpus``/``cards_dir`` default the same way ``search-context`` does (QAR_CORPUS_ROOT / cwd,
    and ``<corpus>/.quest-context`` or QAR_CONTEXT_CARDS_DIR). ``use_llm`` defaults to False
    (keyword/IDF selection only, no model call) so a caller that doesn't ask for it explicitly
    gets an instant, offline result; ``read_only`` stamps no usage heat on the cards it selects
    (``search-context`` uses it; a task being sent does want its cards warmed); pass ``use_llm=True`` to opt into the LLM relevance filter
    (what ``search-context`` does unless its ``--no-llm`` flag is given).

    Best-effort by design: building the model provider or the card store can fail (unconfigured
    backend, unreadable corpus, no cards bootstrapped yet). Any failure here is logged as a
    warning and an empty ``AssembledContext`` (``card_ids == []``) is returned rather than
    raised, so a caller that auto-attaches cards to a task never lets card selection block
    sending the task -- but the failure is not invisible.
    """
    from .adapters.file_context_store import FileContextStore
    from .core.adapters import AssembledContext

    log = logging.getLogger("quest-ai-runner")

    resolved_corpus = corpus or os.getenv("QAR_CORPUS_ROOT") or os.getcwd()
    resolved_cards_dir = (cards_dir or os.getenv("QAR_CONTEXT_CARDS_DIR")
                          or os.path.join(resolved_corpus, ".quest-context"))

    provider = None
    filter_model = None
    if use_llm:
        try:
            from .config import build_orchestrator
            cfg = _config_from_env()
            build_orchestrator(cfg)  # wraps cfg.model_provider with MultiProvider
            provider = cfg.model_provider
            if provider is not None:
                from .core.model_registry import ModelRegistry
                filter_model = ModelRegistry(provider, fallback=cfg.model_fallback or None).resolve_tier("balanced")
        except Exception as e:  # noqa: BLE001 -- fall back to keyword-only card selection
            log.warning("card selection: could not set up LLM relevance filter, "
                        "falling back to keyword/IDF only: %s", e)
            provider = None
            filter_model = None

    try:
        store = FileContextStore(resolved_cards_dir, repo_root=resolved_corpus, auto_bootstrap=False,
                                 provider=provider, model=filter_model)
        return store.assemble(text, read_only=True) if read_only else store.assemble(text)
    except Exception as e:  # noqa: BLE001 -- card selection must never block the caller
        log.warning("card selection failed, sending with no auto-attached cards: %s", e)
        return AssembledContext()


# A corpus at or above this many indexable files gets a first-run confirmation: big enough that
# the user probably wants to know before it runs, small enough that ordinary project roots do not
# trip it.
_LARGE_CORPUS_FILES = 5000


def _count_indexable_files(corpus: str) -> int:
    """How many files one bootstrap of ``corpus`` would index, using the real walk rules."""
    from pathlib import Path as _Path
    from .adapters._walk import effective_skip_dirs, is_claude_state_file, prune_dirnames
    from .adapters.file_context_store import _SOURCE_EXTS
    root = _Path(corpus).resolve()
    skip = effective_skip_dirs(root)
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        prune_dirnames(dirnames, current=_Path(dirpath), base_skip=skip)
        for fn in filenames:
            if not is_claude_state_file(fn) and _Path(fn).suffix.lower() in _SOURCE_EXTS:
                total += 1
    return total


def _cmd_context(args, log) -> int:
    """Run the `context` subcommand; returns the process exit code."""
    import json as _json
    from datetime import datetime, timedelta, timezone

    from .config import build_orchestrator
    from .runner.context_updates import build_update_engine, collect_quest_context

    cfg = _config_from_env(getattr(args, "config", None))
    # Per CLAUDE.md: wrap the provider with MultiProvider before any model call. The relevance
    # judge is a model call, and unwrapped it 404s on half the model ids a deployment pins.
    build_orchestrator(cfg)

    if args.sources:
        engine = build_update_engine(cfg, None, read_only=True)
        if engine is None:
            print("Context updates are switched off for this config "
                  "(RunnerConfig.context_updates / QAR_CONTEXT_UPDATES).")
            return 1
        print("Source names a quest's autopilot.context_sources can use:")
        for name, describes in engine.describe_sources().items():
            print(f"  {name:<16} {describes}")
        return 0

    if args.tracked:
        from .runner.feedback_ledger import build_ledger
        ledger = build_ledger(cfg, state_path=cfg.state_path, read_only=True)
        if ledger is None:
            print("The feedback ledger is switched off for this config "
                  "(RunnerConfig.feedback_ledger / QAR_FEEDBACK_LEDGER).")
            return 1
        rows = sorted(ledger.for_card(args.quest_id),
                      key=lambda i: (not i.is_open, i.occurred_at or i.first_seen_at))
        if not rows:
            print(f"Nothing tracked yet on {args.quest_id}. Items are recorded when a run is "
                  f"handed them, and moved when a run says what it did with them.")
            return 0
        owed = [i for i in rows if i.is_open]
        print(f"{len(rows)} tracked on {args.quest_id}; {len(owed)} still owed.\n")
        for item in rows:
            mark = "OWED" if item.is_open else "    "
            when = (item.occurred_at or item.first_seen_at)[:10] or "undated"
            card = f"  guidance: {item.guidance_card_id}" if item.guidance_card_id else ""
            print(f"  {mark}  {when}  {item.kind:<9} {item.status_line()}{card}")
            print(f"        {' '.join(str(item.text or '').split())[:110]}")
        return 0

    since = None
    if args.since:
        raw = args.since.strip().replace("Z", "+00:00")
        try:
            since = datetime.fromisoformat(raw)
        except ValueError:
            log.error("--since %r is not an ISO-8601 timestamp (e.g. 2026-09-01 or "
                      "2026-09-01T06:00:00Z)", args.since)
            return 1
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    elif args.days is not None:
        # Resolved here rather than inside the call, so the line printed below names the
        # window that was actually used instead of describing the default it replaced.
        since = datetime.now(timezone.utc) - timedelta(days=args.days)

    try:
        bundle = collect_quest_context(args.quest_id, cfg=cfg, since=since,
                                       sources=args.source)
    except ValueError as e:
        log.error("%s", e)
        return 1

    block = bundle.as_prompt_block()
    if args.block:
        print(block)
        return 0
    if args.as_json:
        payload = bundle.as_dict()
        payload["prompt_block"] = block
        print(_json.dumps(payload, indent=2, sort_keys=False))
        return 0

    # A quest's state row often carries an outcome and no name, and ``_card_label`` skips the
    # outcome on purpose (it is a sentence about the future, and it crowds out the manifest
    # column it would sit in). So an unnamed quest is headed by its id ONCE, rather than by the
    # id printed twice as though one of them were a name.
    named = f"{bundle.card_label} ({args.quest_id})" if bundle.card_label else args.quest_id
    window = (f"since {since.isoformat()}" if since
              else "since each source was last delivered to a run")
    print(f"Context for {named}, read at {bundle.collected_at.isoformat()}, {window}.")
    owed = [u for u in bundle.updates if u.kind == "still owed"]
    owed_line = f", {len(owed)} carried over from before" if owed else ""
    print(f"{len(bundle.updates)} update(s), {len(bundle.needing_response())} waiting on an "
          f"answer{owed_line}. Nothing was marked as seen: this is a read.")
    print()
    for r in bundle.reports:
        spec = {k: v for k, v in (r.spec or {}).items() if k != "source"}
        detail = f"  {spec}" if spec else ""
        state = f"ERROR {r.error}" if r.error else f"{r.found} found"
        if getattr(r, "set_aside", 0):
            state += f", {r.set_aside} set aside as not about this work"
        # Why a channel came back empty, when it knows. "0 found" alone reads as broken to
        # somebody who is certain they left a comment on that document yesterday.
        if not r.error and not r.found and r.explanation:
            state += f" ({r.explanation})"
        print(f"  {r.source:<16} {state}{detail}")
    print()
    print(block or "(nothing new on any channel this quest watches)")
    return 0


def _cmd_bootstrap(args, log) -> int:
    """Run the `bootstrap` subcommand; returns the process exit code."""
    from .adapters.card_limit import max_cards_from_env
    from .adapters.file_context_store import FileContextStore
    from .adapters._walk import effective_skip_dirs, prune_dirnames
    import shutil
    from pathlib import Path

    corpus = args.corpus or os.getenv("QAR_CORPUS_ROOT") or os.getcwd()
    cards_dir = args.cards_dir or os.getenv("QAR_CONTEXT_CARDS_DIR") or os.path.join(corpus, ".quest-context")

    # Dry-run mode: run bootstrap without writing cards, track tokens
    if args.dry_run:
        from .adapters.dryrun_provider import DryRunProvider
        from .adapters.file_context_store import FileContextStore
        from .adapters._walk import effective_skip_dirs, prune_dirnames

        # Count source files and estimate areas
        corpus_path = Path(corpus)
        skip_dirs = effective_skip_dirs(corpus_path)
        file_count = 0

        for dirpath, dirnames, filenames in os.walk(corpus_path):
            prune_dirnames(dirnames, current=Path(dirpath).resolve(), base_skip=skip_dirs)
            for fname in filenames:
                if Path(fname).suffix in {
                    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
                    ".java", ".rb", ".md", ".sh", ".yaml", ".yml", ".toml", ".json",
                    ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt", ".scala",
                    ".html", ".css", ".scss", ".less", ".txt", ".rst",
                }:
                    fpath = Path(dirpath) / fname
                    try:
                        if fpath.stat().st_size <= 512 * 1024:  # 512KB max
                            file_count += 1
                    except OSError:
                        pass

        # Run bootstrap with DryRunProvider to track actual tokens
        provider = _model_provider_from_env()
        dryrun_provider = DryRunProvider(provider)

        store = FileContextStore(cards_dir, repo_root=corpus, dry_run=True)
        store.bootstrap(root=corpus, provider=dryrun_provider)

        # Get token counts from the provider
        tokens_in = dryrun_provider.tokens_in
        tokens_out = dryrun_provider.tokens_out
        total_tokens = tokens_in + tokens_out

        # Estimate number of cards using heuristic: ~50 files per card
        estimated_cards = max(1, file_count // 50)

        # Estimate TF-DF-IDF sampling ratio: ~10% of files sampled
        sampled_files = max(10, file_count // 10)
        file_tokens_saved = (file_count - sampled_files) * 20  # ~20 chars per path
        savings_pct = 75  # typical savings with TF-DF-IDF

        # Cost estimate using actual provider/model pricing
        cost, prov, model = estimate_bootstrap_cost(tokens_in)

        # Time estimate: account for LLM call latency
        llm_calls = 2  # Stage 1 + Stage 2
        call_overhead = llm_calls * 4  # ~4s per call
        token_processing = max(10, (total_tokens // 100))  # ~100 tokens/sec
        time_estimate = call_overhead + token_processing

        corpus_abs = str(corpus_path.resolve())
        print()
        print("DRY RUN: Bootstrap Cost Estimate")
        print("=" * 50)
        print(f"Corpus: {corpus_abs}")
        print(f"Source files: {file_count:,}")
        print(f"Estimated areas: {max(5, file_count // 50)}")
        print()
        print("Tokens (with TF-DF-IDF sampling):")
        print(f"  Input tokens: {tokens_in:,}")
        print(f"  Output tokens: {tokens_out:,}")
        print(f"  Vs. full list: {file_tokens_saved:,} tokens saved ({savings_pct}%)")
        print()
        print(f"Provider: {prov}")
        print(f"Model: {model}")
        print()
        print("Cost & Time:")
        print(f"  Estimated cost: ${cost:.4f}")
        print(f"  Estimated time: ~{time_estimate}s (~{time_estimate // 60}m)")
        print()
        print("Run without --dry-run to bootstrap.")
        print()
        return 0

    # Force mode: delete all cards and bootstrap from scratch
    if args.force:
        if os.path.exists(cards_dir):
            log.info("deleting existing cards directory: %s", cards_dir)
            shutil.rmtree(cards_dir)
            log.info("cards deleted")

    import time

    # Prune-only: clean a store WITHOUT paying to re-analyse it. A store can accumulate dead
    # weight faster than anyone wants to re-index a large corpus (a skip list grows, a vendored
    # SDK lands, files are deleted), and making the cleanup conditional on a full bootstrap
    # means it never happens on exactly the corpora where it matters most.
    if args.prune_only:
        store = FileContextStore(cards_dir, repo_root=corpus)
        before = len(store._repo.load_all() or {})
        removed, healed = store.prune_only(root=corpus)
        after = len(store._repo.load_all() or {})
        print()
        print("Prune Complete")
        print("=" * 50)
        print(f"Corpus: {corpus}")
        print(f"Cards before:     {before}")
        print(f"Cards after:      {after}")
        print(f"Removed:          {removed}")
        print(f"Summaries healed: {healed}")
        print()
        return 0

    # FIRST-RUN CONFIRMATION. Indexing is the one operation here whose cost scales with
    # something the user may never have looked at: how many files sit under the root they
    # pointed at. This used to be "handled" by a hardcoded 10,000-file ceiling applied
    # SILENTLY, so a larger corpus was indexed in part and reported as done. Truncating
    # without saying so is the worst of the options; the honest ones are to index everything
    # or to ask. This asks -- once, and only when there is a person there to answer.
    meta_path = os.path.join(cards_dir, "bootstrap_meta.json")
    if (not os.path.exists(meta_path)) and not args.yes:
        n_files = _count_indexable_files(corpus)
        if n_files >= _LARGE_CORPUS_FILES:
            est_min = max(1, round(n_files / 2500))
            print()
            print(f"First bootstrap of {corpus}")
            print(f"  {n_files:,} indexable files - roughly {est_min} minute(s) of model calls.")
            print("  Every file under this root is indexed. Narrow --corpus, or set")
            print("  QAR_BOOTSTRAP_MAX_FILES, if that is more than you meant.")
            if not sys.stdin.isatty():
                # Nobody to ask: proceed, but never silently - the scale is the whole point.
                log.warning("non-interactive: proceeding to index all %d file(s)", n_files)
            else:
                try:
                    reply = input("  Proceed? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    reply = ""
                if reply not in ("y", "yes"):
                    print("  Aborted. Nothing was indexed.")
                    return 0

    provider = _model_provider_from_env()
    store = FileContextStore(cards_dir, repo_root=corpus, max_cards=max_cards_from_env())
    log.info("bootstrapping context store for %s", corpus)

    # Resolve a model from the provider. QAR_MODEL_* is honoured HERE too, not only in
    # _config_from_env: bootstrap is the most expensive command in the tool, so the tier knob
    # silently doing nothing for it meant the one dial that controls that cost was inert.
    # QAR_BOOTSTRAP_TIER picks which tier to use at all (topic extraction is bulk
    # classification, so a deployment may reasonably run it on "fast").
    from .core.model_registry import ModelRegistry
    registry = ModelRegistry(provider)
    tier = os.getenv("QAR_BOOTSTRAP_TIER", "balanced").strip() or "balanced"
    model = os.getenv(f"QAR_MODEL_{tier.upper()}") or registry.resolve_tier(tier)

    start_time = time.time()
    n = store.bootstrap(root=corpus, provider=provider, model=model)
    elapsed_time = time.time() - start_time

    log.info("done: %d cards in %s", n, cards_dir)

    # Display bootstrap summary in nice format
    corpus_abs = str(Path(corpus).resolve())
    tokens_in = getattr(provider, "tokens_in", 0)
    tokens_out = getattr(provider, "tokens_out", 0)
    cost, prov, _estimator_model = estimate_bootstrap_cost(tokens_in)

    print()
    print("Bootstrap Complete")
    print("=" * 50)
    print(f"Corpus: {corpus_abs}")
    print(f"Cards created: {n}")
    print()
    # MEASURED usage first, and the token classes split out, because they bill at very
    # different rates and a single "input" total cannot be turned back into a cost. The
    # modelled estimate is still printed, clearly labelled, since not every provider reports
    # usage -- but where a real number exists it is the one that leads.
    measured = getattr(provider, "cost_usd", 0.0) or 0.0
    calls = getattr(provider, "call_count", 0) or 0
    if tokens_in > 0:
        print("Tokens used (measured):")
        print(f"  Fresh input:    {getattr(provider, 'fresh_input_tokens', 0):,}")
        print(f"  Cache creation: {getattr(provider, 'cache_creation_tokens', 0):,}")
        print(f"  Cache read:     {getattr(provider, 'cache_read_tokens', 0):,}")
        print(f"  Output:         {tokens_out:,}")
        print()
    print(f"Provider: {prov}")
    # The model that ACTUALLY ran, not the one the cost estimator assumed. Printing the
    # estimator's guess here made a --tier override look like it had been ignored.
    print(f"Model: {model}")
    print(f"Tier: {tier}")
    print()
    if measured > 0:
        print(f"Cost (measured, reported by the provider): ${measured:.4f}"
              f"{f' over {calls:,} call(s)' if calls else ''}")
    elif tokens_in > 0:
        print(f"Cost (modelled estimate, provider reported none): ${cost:.4f}")
    print(f"Time: {elapsed_time:.0f}s (~{int(elapsed_time // 60)}m)")
    print()

    # Send monthly email report after bootstrap completion
    try:
        from .adapters.bootstrap_reporter import (
            mark_bootstrap_completed,
            send_bootstrap_report_via_quest,
        )
        from .runner.quest_client import quest_client_from_env

        mark_bootstrap_completed(cards_dir)

        try:
            quest_client = quest_client_from_env(os.environ)
            user_id = os.getenv("QAR_BOOTSTRAP_EMAIL_USER")
            if user_id:
                if send_bootstrap_report_via_quest(
                    cards_dir=cards_dir,
                    corpus_root=corpus_abs,
                    user_id=user_id,
                    quest_client_factory=lambda: quest_client,
                ):
                    log.info("monthly bootstrap report email sent")
                else:
                    log.debug("monthly bootstrap report email not sent (not configured or recently sent)")
        except ImportError:
            log.debug("Quest client not available, skipping monthly bootstrap email")
        except Exception as e:
            log.debug("failed to send monthly bootstrap email: %s", e)
    except Exception as e:
        log.debug("bootstrap reporter setup failed: %s", e)

    return 0


def _cmd_paste_context(args, log) -> int:
    """Run the `paste-context` subcommand; returns the process exit code."""
    import sys
    import hashlib
    from pathlib import Path

    corpus = args.corpus or os.getenv("QAR_CORPUS_ROOT") or os.getcwd()
    cards_dir = args.cards_dir or os.getenv("QAR_CONTEXT_CARDS_DIR") or os.path.join(corpus, ".quest-context")

    # Read context from args or stdin
    if args.context:
        context_text = args.context
    else:
        try:
            context_text = sys.stdin.read()
        except KeyboardInterrupt:
            log.error("interrupted")
            return 1
        except Exception as e:  # noqa: BLE001
            log.error("failed to read stdin: %s", e)
            return 1

    if not context_text.strip():
        log.error("no context provided")
        return 1

    # Determine card id
    if args.card_id:
        card_id = args.card_id
    else:
        # Auto-generate from content hash
        h = hashlib.sha256(context_text.encode("utf-8")).hexdigest()[:8]
        card_id = f"pasted-{h}"

    # Build the card
    from datetime import datetime, timezone
    card = {
        "id": card_id,
        "keywords": ["pasted", "context"],
        "summary": context_text.split('\n')[0][:100],  # First line or 100 chars
        "content": context_text,
        "files": [],
        "conventions": [],
        "provenance": {
            "created_by_task": "paste-context",
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        "usage_count": 0,
        "last_outcome": "unknown",
    }
    if args.goal_id:
        card["goal_id"] = args.goal_id

    # Write through the card repository, the one place that refuses an id that may never be stored.
    from .adapters.card_repository import FilesystemCardRepository
    card_path = Path(cards_dir) / f"{card_id}.json"
    if not FilesystemCardRepository(str(cards_dir)).write(card_id, card):
        log.error("failed to write card %s (see the error above if the id was refused)", card_id)
        return 1

    print(f"Saved to {card_path}")
    print(f"Card ID: {card_id}")
    return 0


def _cmd_search_context(args, log) -> int:
    """Run the `search-context` subcommand; returns the process exit code."""
    result = select_card_ids_for_text(
        args.query,
        corpus=getattr(args, "corpus", None),
        cards_dir=getattr(args, "cards_dir", None),
        use_llm=not getattr(args, "no_llm", False),
        read_only=True,     # a report that only looks: it stamps no usage on any card
    )

    if not result.card_ids:
        print("No context cards matched.")
        return 0

    mode = "IDF only" if getattr(args, "no_llm", False) else "IDF + LLM filter"
    print(f"Query: {args.query}")
    print(f"Found {len(result.card_ids)} card(s)  [{mode}]")
    print()
    for i, m in enumerate(result.card_metadata, 1):
        title = m.get("title") or m["id"]
        score_pct = int(m["relevance_score"] * 100)
        fc = m["file_count"]
        print(f"  {i}. {title}  ({score_pct}% match, {fc} file{'s' if fc != 1 else ''})")
        for f in m.get("files", []):
            print(f"       {f}")
        print()
    if result.sources:
        all_items = []
        for src in result.sources:
            all_items.extend(src.get("items", []))
        if all_items:
            print(f"Top sources ({len(all_items)} total):")
            for f in all_items[:8]:
                print(f"  {f}")
            if len(all_items) > 8:
                print(f"  ... +{len(all_items) - 8} more")
    return 0
