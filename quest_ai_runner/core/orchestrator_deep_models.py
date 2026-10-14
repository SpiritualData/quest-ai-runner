"""Which model each deep attempt uses: the ladder, its fallback and tier resolution.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .deep_model_selection import DEFAULT_AUTO_DEEP_LADDER, normalize_difficulty, select_deep_start
from .model_registry import LEGACY_TIER_ALIASES, ModelRegistry
from .orchestrator_constants import CONFIGURED_LADDER_SOURCE, FALLBACK_LADDER_SOURCE, log
from .orchestrator_decisions import _guidance_model_pref


class DeepModelsMixin:
    """Which model each deep attempt uses: the ladder, its fallback and tier resolution."""

    def _deep_models(self, model_hint: Optional[str], quality_standards: Optional[str],
                     fallback: Optional[str],
                     difficulty: Optional[str] = None,
                     difficulty_reason: Optional[str] = None) -> List[Optional[str]]:
        """The deep-worker ladder only (see ``deep_model_plan`` for the full resolution)."""
        return self.deep_model_plan(model_hint, quality_standards, fallback,
                                    difficulty=difficulty, difficulty_reason=difficulty_reason)[0]

    def deep_model_plan(self, model_hint: Optional[str], quality_standards: Optional[str],
                        fallback: Optional[str], *,
                        difficulty: Optional[str] = None,
                        difficulty_reason: Optional[str] = None,
                        ) -> Tuple[List[Optional[str]], Optional[Dict[str, Any]]]:
        """Resolve the deep-worker ladder plus, when the start was chosen automatically, the
        selection record (difficulty, start_model, ladder, reason) to log and record; None when
        the ladder came from a pin or from the pre-existing resolution unchanged.

        Pins (per-task model, guidance preference) are resolved first and always win. Then, when
        ``cfg.deep_auto_model`` is on and the planner rated the work (``difficulty``), the run
        starts on that difficulty's rung of the configured ladder (or ``DEFAULT_AUTO_DEEP_LADDER``)
        and escalates from there. With no rating the configured ladder starts at the NORMAL
        model's rung when it has one ("when unsure, normal"), and with no configured ladder either,
        today's generic fallback ladder is used unchanged."""
        ladder, pinned = self.resolve_deep_ladder(model_hint, quality_standards, fallback,
                                                  log_ladder=False)
        if pinned:
            return ladder, None   # a pin logs its own line inside the resolver
        if not self.cfg.deep_auto_model:
            self.log_deep_ladder(ladder, source=(CONFIGURED_LADDER_SOURCE if self.cfg.deep_model_ladder
                                                 else FALLBACK_LADDER_SOURCE))
            return ladder, None
        configured = [m for m in (self.cfg.deep_model_ladder or []) if m]
        level = normalize_difficulty(difficulty)
        if level is not None:
            base = configured or list(DEFAULT_AUTO_DEEP_LADDER)
            picked = select_deep_start(level, base, self.cfg.deep_difficulty_models,
                                       reason=difficulty_reason)
        elif configured:
            picked = select_deep_start("normal", configured, self.cfg.deep_difficulty_models,
                                       reason="difficulty not assessed; using the normal start")
            if picked is not None:
                picked[1]["difficulty"] = None
        else:
            picked = None
        if picked is None:
            self.log_deep_ladder(ladder, source=(CONFIGURED_LADDER_SOURCE if configured
                                                 else FALLBACK_LADDER_SOURCE))
            return ladder, None
        start_ladder, selection = picked
        log.info("Deep-worker model auto-selected: difficulty=%s, starting model=%s, escalation "
                 "ladder=%s, reason: %s", selection.get("difficulty") or "not assessed",
                 selection["start_model"], selection["ladder"], selection["reason"])
        return list(start_ladder), selection

    def resolve_deep_ladder(self, model_hint: Optional[str], quality_standards: Optional[str],
                            fallback: Optional[str], *,
                            log_ladder: bool = True) -> Tuple[List[Optional[str]], bool]:
        """Resolve the deep-worker model LADDER (tried in order, escalating on a not-met goal), and
        whether it is a PIN (``(ladder, pinned)``; a pin is never re-started by difficulty).

        Priority: (1) an explicit per-task model request via ``model_hint`` when it names a model the
        worker can run; (2) a guidance card model preference; either PINS a single model (no
        escalation). Otherwise (3) the configured ``deep_model_ladder`` (the consumer's explicit
        ladder, e.g. from ``QAR_DEEP_MODELS`` -- REAL Claude ids/aliases, weak -> strong), else
        (4) a ladder built from the single ``fallback`` model the orchestrator was given, EXTENDED
        with any genuinely DIFFERENT Claude-runnable id found by resolving the "quality"/"best"
        tiers (so a deployment whose tier config names a stronger Claude model -- e.g.
        ``QAR_MODEL_BEST=claude-opus-4-8``, or a CLI-only lane's ``claude-opus`` bucket -- gets a
        real escalation step instead of a silently inert length-1 ladder; see
        ``fallback_deep_ladder``). Always returns a non-empty list.
        Logs (INFO) the resolved ladder once per deep run, and WARNS when a NON-pinned resolution
        still comes out length <= 1 (escalation unavailable), so a deployment can see and fix it."""
        from .goal_runner import _is_claude_model, cli_safe_model  # deep worker is Claude Code
        # Explicit per-task model request that names a MODEL rather than a tier (e.g. a quest's
        # ``autopilot.model = "fable"``, or a task's stored ``model``): honour it VERBATIM.
        #
        # ``fallback`` cannot be used for this, even though it was derived from ``model_hint``:
        # it is the hint AFTER ``ModelRegistry.resolve_tier``, which only understands tier names
        # and silently rewrites anything else to the balanced tier. So a lane asking for Fable
        # arrived here as its balanced model and was then "pinned" to that, with an INFO line
        # calling it "an explicit per-task model request" -- the request's own name was gone.
        # (Live: the dissertation lane pinned ``fable`` and ran every deep task on Sonnet, which
        # needs more turns for the same work and so kept hitting the turn wall.)
        #
        # Tiers still resolve through the registry exactly as before: ``cli_safe_model`` returns
        # None for the four semantic tier names, and ``is_tier_name`` keeps the legacy aliases
        # (haiku/sonnet/opus), which ARE tier names, out of this branch.
        if model_hint and not ModelRegistry.is_tier_name(model_hint):
            pinned = cli_safe_model(model_hint)
            if pinned:
                log.info("Deep-worker model ladder: pinned to %r (explicit per-task model id, used "
                         "verbatim); escalation intentionally disabled.", pinned)
                return [pinned], True
        # A per-task request that names a Claude FAMILY by its legacy alias ("opus", "sonnet",
        # "haiku") means that family for the deep worker, even though the registry reads the same
        # word as a tier. When this lane's tier config resolves that tier to a DIFFERENT family,
        # the family the request named wins. Live (2026-10-06): the SD shared lane pins every
        # shallow tier to haiku (QAR_MODEL_QUALITY=haiku, so cheap planning stays cheap), and an
        # autopilot thread whose deep_run_model is "opus" was run on haiku, logged as "an
        # explicit per-task model request". A tier that resolves within the family (opus ->
        # claude-opus-4-8) is still honoured as resolved, so a lane's version pin keeps working.
        requested_family = (model_hint or "").strip().lower()
        if (requested_family in LEGACY_TIER_ALIASES and fallback and _is_claude_model(fallback)
                and requested_family not in str(fallback).lower()):
            log.info("Deep-worker model ladder: pinned to %r (explicit per-task model request; "
                     "this lane's tier for it resolves to %r, a different Claude family, so the "
                     "family the request names is used); escalation intentionally disabled.",
                     requested_family, fallback)
            return [requested_family], True
        # A per-task TIER request (or a model id this worker cannot run): ``fallback`` is that
        # request resolved through the registry. Pin it when the worker can run it; in a non-Claude
        # deployment the resolved hint is not worker-runnable, so we fall through to the ladder
        # instead of handing Claude Code a Gemini/OpenAI id.
        if model_hint and fallback and _is_claude_model(fallback):
            log.info("Deep-worker model ladder: pinned to %r (explicit per-task model request); "
                     "escalation intentionally disabled.", fallback)
            return [fallback], True
        pref = _guidance_model_pref(quality_standards)
        if pref:
            log.info("Deep-worker model ladder: pinned to %r (guidance model preference); "
                     "escalation intentionally disabled.", pref)
            return [pref], True
        if self.cfg.deep_model_ladder:
            ladder = [m for m in self.cfg.deep_model_ladder if m]
            if ladder:
                if log_ladder:
                    self.log_deep_ladder(ladder, source=CONFIGURED_LADDER_SOURCE)
                return list(ladder), False
        ladder = self.fallback_deep_ladder(fallback)
        if log_ladder:
            self.log_deep_ladder(ladder, source=FALLBACK_LADDER_SOURCE)
        return ladder, False

    def fallback_deep_ladder(self, fallback: Optional[str]) -> List[Optional[str]]:
        """Build the deep-worker ladder when no explicit ``deep_model_ladder`` is configured.

        Starts from the single ``fallback`` model the orchestrator was already given, then tries to
        EXTEND it with a real escalation step by resolving the "quality" then "best" tiers through
        the registry: on many deployments these name a genuine Claude id (the library's own
        last-known default, or an explicit operator override like ``QAR_MODEL_BEST=claude-opus-4-8``
        -- see HANDS_FREE_QUEST_AI_DESIGN.md section 2, point 4), which is what makes "goal not met
        -> stronger model" do something at all.

        The extension is attempted for EVERY fallback, not only a non-Claude one. It used to be
        skipped whenever the fallback was already Claude-shaped, on the reasoning that such a
        deployment had picked its deep model -- but "Claude-shaped" and "a distinct rung worth
        escalating to" are different questions. A CLI-only deployment resolves its tiers to family
        BUCKET LABELS (``claude-sonnet``), so the guard saw Claude, stopped at one rung, and the
        goal loop's attempt-N indexing re-ran the identical model on every retry: the escalation
        mechanism ran, with nothing to escalate to. Rungs are deduped by what the worker would
        ACTUALLY invoke (``cli_safe_model``: ``claude-sonnet``, ``sonnet`` and ``claude-sonnet-4-6``
        are one rung, not three), so this adds a step only when it is a genuinely different model.

        Sonnet is the balanced tier's CLI family (``DEFAULT_FALLBACK_TOP["best"]`` pins
        ``claude-sonnet``; see ``core/model_registry.py``), so a CLI-only deployment whose fallback
        already sits at the ``claude-sonnet`` bucket has genuinely nothing further to escalate to via
        auto-resolution -- "quality" and "best" both resolve back onto the same "sonnet" CLI
        invocation, and the tier loop above correctly finds nothing new. That is a real
        ceiling, not a bug: it surfaces as the "Escalation unavailable" WARNING (``log_deep_ladder``
        below), same as a deployment that pins a single explicit model on purpose. Do NOT paper
        over it by inventing a further family (e.g. Fable) as an automatic "stronger than Opus"
        rung -- Opus is the library's best tier, and a consumer that genuinely wants a different
        family in the ladder says so explicitly via ``QAR_DEEP_MODELS``/``QAR_MODEL_BEST``.

        Never raises; always returns a non-empty list (falls back to ``[fallback]`` alone, even if
        that is not Claude-runnable, so a caller always has something to try)."""
        from .goal_runner import _is_claude_model, cli_safe_model  # worker-runnable check + translation

        def rung_key(m: Optional[str]) -> str:
            """What this id resolves to at invoke time — the identity a ladder rung really has."""
            try:
                return (cli_safe_model(m) or m or "").strip().lower()
            except Exception:  # noqa: BLE001 — an untranslatable id is just its own rung
                return (m or "").strip().lower()

        def claude_rank(m: Optional[str]) -> Optional[int]:
            """Weak -> strong among the Claude families (haiku < sonnet < opus); None otherwise."""
            low = (rung_key(m) or "").lower()
            for rank, family in enumerate(("haiku", "sonnet", "opus")):
                if family in low:
                    return rank
            return None

        ladder: List[str] = [fallback] if fallback else []
        seen = {rung_key(m) for m in ladder}
        fallback_rank = claude_rank(fallback)
        for tier in ("quality", "best"):
            try:
                resolved = self.registry.resolve_tier(tier)
            except Exception:  # noqa: BLE001 — an unresolvable tier is just skipped
                continue
            # A rung must be a step UP. A tier that resolves to the same family or a weaker one
            # (quality is sonnet on a deployment whose fallback is already opus) is not escalation.
            resolved_rank = claude_rank(resolved)
            if fallback_rank is not None and resolved_rank is not None and resolved_rank <= fallback_rank:
                continue
            if resolved and _is_claude_model(resolved) and rung_key(resolved) not in seen:
                ladder.append(resolved)
                seen.add(rung_key(resolved))
        return ladder or [fallback]

    @staticmethod
    def log_deep_ladder(ladder: List[Optional[str]], *, source: str) -> None:
        """Log the resolved deep-worker ladder once per deep run (called from ``_deep_models``).

        A ladder this reports on is NEVER an intentional pin (those log their own INFO line and
        return before reaching here) -- so length <= 1 here genuinely means "goal not met -> a
        stronger model" has nothing to escalate to, worth a WARNING naming the fix. Never raises."""
        try:
            log.info("Deep-worker model ladder resolved (%s): %s", source, ladder)
            if len(ladder) <= 1:
                log.warning(
                    "Escalation unavailable: no additional Claude-runnable model configured for the "
                    "deep-worker ladder (resolved: %s). The deep worker (Claude Code) can only "
                    "escalate to another Claude model -- set QAR_DEEP_MODELS to a comma-separated "
                    "list of Claude model ids/aliases (e.g. \"sonnet,opus\"), or point a tier "
                    "override (e.g. QAR_MODEL_BEST) at a real Claude id.", ladder)
        except Exception:  # noqa: BLE001 — logging must never break a deep run
            pass

    def _resolved_deep_tier(self, tier: Optional[str],
                            deep_models: List[Optional[str]]) -> Optional[str]:
        """Resolve a verifier-requested ``next_tier`` to a worker-runnable model id via the registry.

        The deep worker is Claude Code, so the model must be a Claude id. When the tier resolves to
        a Claude model we use it; otherwise (e.g. a Gemini/OpenAI deployment whose tiers map to
        non-Claude ids) we fall back to the STRONGEST model already on the deep ladder so we never
        hand the Claude worker a foreign id. Returns ``None`` if ``tier`` is empty or unresolvable,
        so the caller keeps stepping the ladder instead. Never raises."""
        if not (tier or "").strip():
            return None
        from .goal_runner import _is_claude_model  # worker-runnable check
        try:
            resolved = self.registry.resolve_tier(tier)
        except Exception:  # noqa: BLE001 — an unknown tier just means "no override"
            return None
        if resolved and _is_claude_model(resolved):
            return resolved
        # Non-Claude deployment: pick the strongest Claude model already on the ladder, if any.
        for m in reversed(deep_models):
            if m and _is_claude_model(m):
                return m
        return None
