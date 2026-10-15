"""Non-rendering command handlers: model, depth, system, model-tier menu, preamble and rep skill loading.

A mixin of ``InteractiveSession`` (see ``interactive_session.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import os
from typing import Optional

from .interactive_session_render import _parse_skill_frontmatter


class CommandsMixin:
    """Non-rendering command handlers: model, depth, system, model-tier menu, preamble and rep skill loading."""

    def _effective_preamble(self) -> Optional[str]:
        """Combine the system prompt, persona and standing next steps into the rep_preamble passed
        to the orchestrator.

        This is where the quest folder's standing next-steps artifact becomes structural rather
        than retrievable. rep_preamble reaches the planner, the answer and the deep preamble on
        every turn, so an answer to "what should I do next here" can no longer depend on the
        artifact happening to out-score the rest of the corpus in a card search. It goes LAST, after
        the persona: the rep's identity is what the judge's truncated lens most needs to keep.
        """
        parts = []
        if self._system:
            parts.append(self._system)
        if self._persona:
            parts.append(self._persona)
        standing = getattr(self, "_standing_next_steps", None)
        if standing is not None:
            try:
                from .runner.session_next_steps import render_standing_next_steps
                block = render_standing_next_steps(standing)
            except Exception:  # noqa: BLE001 — never cost a turn its persona over this
                block = ""
            if block:
                parts.append(block)
        return "\n\n".join(parts) if parts else None

    # -- New command handlers --------------------------------------------------

    _DEPTH_ALIASES = {
        "light": "fast", "quick": "fast",
        "standard": "balanced", "normal": "balanced", "default": "balanced",
        "deep": "quality", "thorough": "quality", "hard": "quality",
    }

    _VALID_TIERS = {"fast", "balanced", "quality", "best", "auto"}

    def _cmd_model(self, arg: str) -> None:
        """Set the model tier directly (no arg resets to auto)."""
        c = self._console
        if not arg:
            current = self._model_hint or "auto (orchestrator decides)"
            c.dim(f"  Model: {current}")
            c.dim("  Usage: /model fast | balanced | quality | best  — or /models for a menu")
            return
        tier = arg.lower().strip()
        if tier in ("auto", "reset", "clear"):
            self._model_hint = None
            c.dim("  Model set to auto (orchestrator decides).")
            self._persist_session_state()
            return
        if tier not in self._VALID_TIERS:
            c.dim(f"  Unknown tier {tier!r}. Choose: fast, balanced, quality, best")
            return
        self._model_hint = tier
        c.dim(f"  Model set to {tier}.")
        self._persist_session_state()

    def _cmd_depth(self, arg: str) -> None:
        """Alias for /model using friendlier level names."""
        c = self._console
        if not arg:
            current = self._model_hint or "auto"
            c.dim(f"  Depth/model: {current}")
            c.dim("  Usage: /depth light | standard | deep  —  or /models for the full menu")
            return
        level = arg.lower().strip()
        tier = self._DEPTH_ALIASES.get(level, level)
        if tier not in self._VALID_TIERS:
            c.dim(f"  Unknown depth {level!r}. Choose: light (fast), standard (balanced), deep (quality)")
            return
        self._model_hint = tier
        c.dim(f"  Depth set to {level} (model: {tier}).")

    def _cmd_system(self, arg: str) -> None:
        """Show or set a custom system prompt prepended to the persona."""
        c = self._console
        if not arg:
            if self._system:
                preview = self._system[:120] + "…" if len(self._system) > 120 else self._system
                c.dim(f"  System prompt ({len(self._system)} chars): {preview}")
                c.dim("  Use '/system clear' to remove, or '/system <text>' to replace.")
            else:
                c.dim("  No custom system prompt set.")
                c.dim("  Usage: /system <text>  —  sets a custom prompt prepended to the persona.")
            return
        if arg.lower() == "clear":
            self._system = None
            c.dim("  System prompt cleared.")
            return
        self._system = arg
        kb = max(1, len(arg.encode()) // 1024)
        c.dim(f"  System prompt set ({kb}KB).")

    # -- Model tier menu data (the UI renders and picks) -----------------------

    def _build_model_tiers_menu(self) -> None:
        """Build dynamic model tier menu from provider's actual models."""
        from .core.model_registry import ModelRegistry
        registry = self._orch.registry
        models = registry.top_models()
        # Build menu with semantic tiers and actual model names
        self._model_tiers = [
            ("auto",      "orchestrator decides (recommended)"),
            ("fast",      f"light-weight tasks — {models['fast']}"),
            ("balanced",  f"general chat and coding — {models['balanced']}"),
            ("quality",   f"thorough, deep reasoning — {models['quality']}"),
            ("best",      f"best available (if needed) — {models['best']}"),
        ]

    def _skills_dir(self) -> Optional[str]:
        """Resolve the local skills directory: QAR_SKILLS_DIR > corpus_root/.claude/skills > cwd/.claude/skills."""
        import os
        # Explicit env var takes priority
        explicit = os.getenv("QAR_SKILLS_DIR")
        if explicit:
            return explicit
        # Next, try corpus_root if configured
        corpus = getattr(self._cfg, "corpus_root", None)
        if corpus:
            return os.path.join(corpus, ".claude", "skills")
        # Finally, default to current working directory
        cwd_skills = os.path.join(os.getcwd(), ".claude", "skills")
        if os.path.isdir(cwd_skills):
            return cwd_skills
        # If none of the above exist, still return cwd path (for /reps to show the hint)
        return cwd_skills

    def _refresh_rep_name_from_skill(self) -> None:
        """Update _rep_name from the loaded skill file's display_name/name field."""
        if not self._persona_file:
            return
        meta = _parse_skill_frontmatter(self._persona_file)
        name = meta.get("display_name") or meta.get("name")
        if name:
            self._rep_name = name

    def _try_load_skill_by_name(self, name: str) -> None:
        """Auto-load SKILL.md for a rep name if discoverable, and update display name."""
        if not name or name in ("AI", "Assistant"):
            return
        skills_dir = self._skills_dir()
        if not skills_dir:
            return
        skill_file = os.path.join(skills_dir, name, "SKILL.md")
        if not os.path.isfile(skill_file):
            return
        try:
            self._persona = open(skill_file).read()  # noqa: WPS515
            self._persona_file = skill_file
            self._refresh_rep_name_from_skill()
        except OSError:
            pass
