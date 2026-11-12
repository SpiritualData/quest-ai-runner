"""Which model each deep attempt uses: the ladder, its fallback and tier resolution.

A mixin of ``Orchestrator`` (see ``orchestrator.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.deep_model_selection import (
    choice_of, escalate_choice, normalize_difficulty, plan_start, pure_tier, tier_models, tier_name_of, tier_of_model)
from quest_ai_runner.the_intelligence.mind.delegation.work_model import rung_allowed
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import LEGACY_TIER_ALIASES, ModelRegistry
from quest_ai_runner.the_intelligence.mind.orchestrator_constants import CONFIGURED_LADDER_SOURCE, FALLBACK_LADDER_SOURCE, log
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import _guidance_model_pref


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
                        task_kind: Optional[str] = None,
                        requested_tier: Optional[str] = None,
                        standing_model: Optional[str] = None,
                        allowed_models: Optional[List[str]] = None,
                        ) -> Tuple[List[Optional[str]], Optional[Dict[str, Any]]]:
        """Resolve the deep-worker ladder plus, when the start was chosen by the hub's tier rule, the
        selection record (difficulty, start_model, ladder, reason, tier, tier_source, tier_reason,
        tier_role) to log and record; None when the ladder came from a pin or from the pre-existing
        resolution unchanged.

        A specific MODEL (per-task ``deep_run_model``, a message's model, a guidance preference naming
        a model id) is a PIN: it starts that model alone and never escalates. A TIER the person picked
        (``requested_tier``, or a tier word in ``model_hint``) is the START tier instead: it beats the
        standing guidance and the default, and an escalation may still move one rung from it. Otherwise,
        with ``cfg.deep_auto_model`` on, the hub decides (see ``deep_model_selection.plan_start``): a
        guidance card that names a tier is the person's standing guidance, then the environment's default
        work model (``standing_model``), then an operator's ``QAR_DEEP_MODEL_*``, and with none of them
        the hub's default (the cheapest tier measured good enough) starts the run. ``allowed_models`` is
        the environment's allowed list, applied to every rung. The planner's ``difficulty`` is
        information: with a specific ``difficulty_reason`` and a hard rating it asks for one tier up,
        otherwise it changes nothing. Nothing here is written to the task."""
        pref = _guidance_model_pref(quality_standards)
        standing_tier = None
        guidance = quality_standards
        tier_request = pure_tier(requested_tier) or (pure_tier(model_hint) if self.cfg.deep_auto_model else "")
        pin_hint = None if (tier_request and pure_tier(model_hint)) else model_hint
        if self.cfg.deep_auto_model and tier_request:
            guidance = None   # the person's own tier outranks a standing card, whatever the card names
        elif self.cfg.deep_auto_model and pref and ModelRegistry.is_tier_name(pref):
            standing_tier = tier_name_of(pref)
            if standing_tier:
                guidance = None   # a tier is handed to the hub below, not pinned
        ladder, pinned = self.resolve_deep_ladder(pin_hint, guidance, fallback, log_ladder=False)
        if pinned:
            return ladder, None   # a pin logs its own line inside the resolver
        configured = [m for m in (self.cfg.deep_model_ladder or []) if m]
        if not self.cfg.deep_auto_model:
            self.log_deep_ladder(ladder, source=(CONFIGURED_LADDER_SOURCE if configured else FALLBACK_LADDER_SOURCE))
            return ladder, None
        level = normalize_difficulty(difficulty)
        operator_level = level or "normal"
        operator_model = (self.cfg.deep_difficulty_models or {}).get(operator_level)
        operator_var = f"QAR_DEEP_MODEL_{operator_level.upper()}"
        operator_label = ""
        if standing_model and not standing_tier and not tier_request:
            # The environment's default work model is standing guidance below the person's own card and above the operator's variable: its tier when it
            # names or resolves to one, else the model itself.
            standing_tier = tier_name_of(standing_model) or tier_of_model(standing_model, tier_models(self.registry)) or None
            if not standing_tier:
                operator_model, operator_var, operator_label = standing_model, "", "the environment's default work model"
        allows = None if allowed_models is None else (lambda rung: rung_allowed(rung, allowed_models))
        plan = plan_start(self.registry, configured_ladder=configured, task_kind=(task_kind or ""), standing_tier=standing_tier,
                          operator_model=operator_model, operator_var=operator_var, operator_label=operator_label,
                          difficulty=difficulty, difficulty_reason=difficulty_reason,
                          requested_tier=tier_request or None, allows=allows)
        if plan is None and allows is not None:
            log.warning("Deep-worker tier: no rung of the ladder is on the environment's allowed models; the resolved ladder is used")
        if plan is None:
            self.log_deep_ladder(ladder, source=(CONFIGURED_LADDER_SOURCE if configured else FALLBACK_LADDER_SOURCE))
            return ladder, None
        selection = plan.selection
        log.info("Deep-worker model chosen by the tier rule: tier=%s, tier_source=%s, tier_reason=%s%s, starting model=%s, escalation ladder=%s, "
                 "difficulty=%s", selection.get("tier") or "(operator model)", selection.get("tier_source"), selection.get("tier_reason"),
                 f", tier_from={selection['tier_from']}" if selection.get("tier_from") else "", selection["start_model"], selection["ladder"],
                 selection.get("difficulty") or "not assessed")
        return list(plan.ladder), selection

    def escalate_deep_choice(self, selection: Optional[Dict[str, Any]], run_model: Optional[str], signal: str,
                             finding: Optional[str]) -> Optional[Dict[str, str]]:
        """The tier record of ONE task's escalation (one rung up, with the finding that says what was missing), or None when it must not
        move. Goes through the hub's ``escalate``; nothing is stored, so the next task is chosen again."""
        try:
            models = tier_models(self.registry)
        except Exception:  # noqa: BLE001
            models = {}
        moved = escalate_choice(choice_of(selection, run_model, models), signal, finding)
        if moved is None:
            return None
        record = dict(moved.record())
        record["tier_role"] = moved.role
        log.info("Deep-worker tier escalated for this task only: tier=%s, tier_source=%s, tier_reason=%s, tier_from=%s",
                 record.get("tier"), record.get("tier_source"), record.get("tier_reason"), record.get("tier_from"))
        return record

    def typed_model_request(self, requested: Optional[str]) -> Optional[str]:
        """The model the person named in their message, when the deep worker can run it, else None.

        A tier word (fast/balanced/quality/best) or a Claude family or id passes; a name this
        worker cannot run (another provider's model) never reaches ``claude --model``, so the run
        keeps its standing choice and the caller says so."""
        from quest_ai_runner.the_intelligence.mind.delegation.goal_runner import cli_safe_model
        # The planner copies the person's words, so "the best tier" or "opus model" arrive as
        # written; the name inside them is what counts (live 2026-11-10: "best tier" pinned nothing).
        want = re.sub(r"\s+(tier|model)$", "", re.sub(r"^(the|a)\s+", "", (requested or "").strip().lower()))
        if not want:
            return None
        if ModelRegistry.is_tier_name(want) or cli_safe_model(want):
            return want
        return None

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
        from quest_ai_runner.the_intelligence.mind.delegation.goal_runner import _is_claude_model, cli_safe_model  # deep worker is Claude Code
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
        from quest_ai_runner.the_intelligence.mind.delegation.goal_runner import _is_claude_model, cli_safe_model  # worker-runnable check + translation

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
        for tier in ("quality",):   # best is never an escalation rung: it is a person's explicit pin or the structure watch's alone
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
