"""Session persistence: transcript, session files, state save and load, and the sessions list.

A mixin of ``InteractiveSession`` (see ``interactive_session.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


class PersistenceMixin:
    """Session persistence: transcript, session files, state save and load, and the sessions list."""

    # -- one turn --------------------------------------------------------------

    def _last_transcript(self) -> str:
        """Return the immediately preceding exchange as a minimal transcript string."""
        if not self._session_history:
            return ""
        user, asst = self._session_history[-1]
        return f"User: {user}\nAssistant: {asst}"

    def _write_session_file(self) -> None:
        """Persist session history to disk so future sessions can recall it via the adapter."""
        if not self._session_file or not self._session_history:
            return
        try:
            messages = []
            for user_text, asst_text in self._session_history:
                messages.append({"role": "user", "content": user_text})
                messages.append({"role": "assistant", "content": asst_text})
            # Metadata lets `chat --resume` pick this corpus's latest conversation and restore
            # the goal it was attached to. Readers of the file only look at "messages".
            payload = {
                "messages": messages,
                "corpus_root": getattr(self._cfg, "corpus_root", None),
                # The selected quest, so `chat --resume` comes back to it.
                "goal_id": self.bound_quest_id() if hasattr(self, "pinned_quest") else self._goal_id,
                "rep_name": self._rep_name,
                "updated_at": time.time(),
            }
            self._session_file.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    # -- qar_state.json session state persistence -----------------------------

    def _state_path(self) -> Path:
        """Path to qar_state.json — same file the poller uses, different key."""
        import os
        return Path(os.getenv("QAR_STATE_PATH", "./qar_state.json"))

    def _persist_session_state(self) -> None:
        """Write model_hint/rep_name/persona_file into qar_state.json chat_state (best-effort)."""
        try:
            path = self._state_path()
            state: dict = {}
            if path.exists():
                try:
                    state = json.loads(path.read_text())
                except (json.JSONDecodeError, OSError):
                    state = {}
            state["chat_state"] = {
                "model_hint": self._model_hint,
                "rep_name": self._rep_name,
                "persona_file": self._persona_file,
                "quest_match": getattr(self, "quest_match_mode", "auto"),
            }
            path.write_text(json.dumps(state, indent=2))
        except Exception:  # noqa: BLE001
            pass  # persistence is best-effort; never block the session

    def _load_session_state(self) -> None:
        """Restore model_hint/rep_name/persona from qar_state.json chat_state (best-effort)."""
        try:
            path = self._state_path()
            if not path.exists():
                return
            state = json.loads(path.read_text())
            cs = state.get("chat_state")
            if not isinstance(cs, dict):
                return
            if cs.get("model_hint"):
                self._model_hint = cs["model_hint"]
            if cs.get("rep_name"):
                self._rep_name = cs["rep_name"]
            if cs.get("quest_match") in ("auto", "none"):
                # The person's own saved choice beats the deployment's QAR_QUEST_AUTO_MATCH default.
                self.saved_quest_match_mode = cs["quest_match"]
            pf = cs.get("persona_file")
            if pf:
                try:
                    self._persona = open(pf).read()  # noqa: WPS515
                    self._persona_file = pf
                    self._refresh_rep_name_from_skill()
                except OSError:
                    pass  # file moved or deleted — skip silently
        except Exception:  # noqa: BLE001
            pass  # best-effort; never block startup

    # -- Session save/load ----------------------------------------------------

    def _sessions_path(self) -> Path:
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        return self._sessions_dir

    def _cmd_save(self, arg: str) -> None:
        """Save the current session to disk."""
        c = self._console
        name = arg.strip() or "default"
        # Sanitize: only alphanumeric, dash, underscore, dot
        import re as _re
        name = _re.sub(r"[^A-Za-z0-9._-]", "_", name) or "default"
        path = self._sessions_path() / f"{name}.json"
        payload = {
            "rep_name": self._rep_name,
            "persona": self._persona,
            "goal_id": self._goal_id,
            "system": self._system,
            "model_hint": self._model_hint,
            "last_user": self._last_user,
            "last_assistant": self._last_assistant,
            "session_history": self._session_history,
            "turn_count": self._turn_count,
            "turns": self._turns,
        }
        try:
            path.write_text(json.dumps(payload, indent=2, default=str))
            c.dim(f"  Session saved: {path}")
        except OSError as e:
            c.dim(f"  Could not save session: {e}")

    def _cmd_load(self, arg: str) -> None:
        """Load a saved session from disk."""
        c = self._console
        name = arg.strip()
        if not name:
            c.dim("  Usage: /load <name>   (see /sessions for saved sessions)")
            return
        import re as _re
        name = _re.sub(r"[^A-Za-z0-9._-]", "_", name)
        path = self._sessions_path() / f"{name}.json"
        if not path.exists():
            c.dim(f"  Session not found: {name!r}  (use /sessions to list)")
            return
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            c.dim(f"  Could not load session: {e}")
            return
        self._rep_name = payload.get("rep_name") or self._rep_name
        self._persona = payload.get("persona")
        self._goal_id = payload.get("goal_id")
        self._system = payload.get("system")
        self._model_hint = payload.get("model_hint")
        self._last_user = payload.get("last_user") or ""
        self._last_assistant = payload.get("last_assistant") or ""
        raw_history = payload.get("session_history") or []
        self._session_history = [tuple(t) for t in raw_history if isinstance(t, (list, tuple)) and len(t) == 2]
        # Backward compat: if no session_history saved but last_user/assistant exist, seed history.
        if not self._session_history and self._last_user:
            self._session_history = [(self._last_user, self._last_assistant)]
        self._turn_count = payload.get("turn_count") or 0
        self._turns = payload.get("turns") or []
        c.dim(f"  Session loaded: {name!r}  ({self._turn_count} prior turns)")
        c.dim(f"  Rep: {self._rep_name}" + (f"  Model: {self._model_hint}" if self._model_hint else ""))

    def _cmd_sessions(self) -> None:
        """List saved sessions."""
        c = self._console
        try:
            paths = sorted(self._sessions_path().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            paths = []
        if not paths:
            c.dim("  No saved sessions.  Use /save <name> to save one.")
            return
        c.line("")
        for p in paths:
            try:
                sz = p.stat().st_size
                data = json.loads(p.read_text())
                rep = data.get("rep_name") or "?"
                turns = data.get("turn_count") or 0
                goal = data.get("goal_id") or ""
                suffix = f"  goal: {goal}" if goal else ""
                c.dim(f"  {p.stem:20s}  {rep:12s}  {turns} turns  ({sz//1024+1}KB){suffix}")
            except Exception:  # noqa: BLE001
                c.dim(f"  {p.stem}")
        c.dim("")
        c.dim("  /load <name>  to restore a session")
