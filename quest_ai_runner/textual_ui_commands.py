"""Prompt submission, slash-command dispatch, the interactive pickers and the command menu.

A mixin of ``QuestAITerminal`` (see ``textual_ui.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import os
from typing import Callable, List, Optional
from textual.widgets.option_list import Option
from textual.widgets import OptionList, TextArea
from rich.text import Text

from .interactive_session import _HELP, _parse_skill_frontmatter
from .textual_ui_prompt import PromptTextArea
from .textual_ui_widgets import _busy_placeholder


class CommandsMixin:
    """Prompt submission, slash-command dispatch, the interactive pickers and the command menu."""

    # -- input dispatch --------------------------------------------------------

    def on_prompt_text_area_submitted(self, event: PromptTextArea.Submitted) -> None:
        line = (event.value or "").strip()
        event.textarea.clear()
        event.textarea.styles.height = 3
        if not line:
            return

        # Any submitted message answers a pending decision question, if there was one.
        self._awaiting_decision = False

        # Session still initializing — queue the message for replay once ready.
        if self.sess is None:
            if line.startswith("/"):
                self._console.dim(f"  Still starting up; {line.split()[0]} opens in a moment.")
            else:
                # Echo it now (it is replayed without a second echo) so it does not seem lost.
                self._tlog.write(Text(f"❯ {line}", style="bold cyan"))
                self._tlog.write(Text(""))
                self._console.dim("  Queued. Sends as soon as the session is ready.")
            self._pre_session_queue.append(line)
            return

        # Menu selection mode (a picker is awaiting a number).
        if self._pending_select is not None:
            cb = self._pending_select
            self.close_choices()
            cb(line)
            return

        # A "/" line is a command for this terminal, never words for the AI: it runs now, even in
        # the middle of a turn (a queued "/quest" used to be handed to the running turn to act on).
        if line.startswith("/"):
            self._dispatch_command(line)
            return

        if self._turn_active:
            # Queue the message. The orchestrator may drain and fold it into THIS turn's own
            # goal loop (a long deep run mid-step); whatever it leaves untouched is picked up
            # and acted on the moment this turn ends (see _finish_turn) — a queued message is
            # never simply dropped once the AI stops, even outside a loop that drains it itself.
            inbox = getattr(self.sess._orch, "input_inbox", None)
            if inbox is None:
                self._tlog.write(Text(
                    f"  ↑ {line}  [not queued: no input inbox wired for this session]",
                    style="dim red",
                ))
                return
            inbox.push(self._session_id, line)
            count = len(inbox.peek(self._session_id))
            self._tlog.write(Text(f"  ↑ queued ({count}): {line}", style="dim"))
            self._activity.set_queued(count)
            self.query_one("#prompt", PromptTextArea).placeholder = _busy_placeholder(count)
            return

        self._begin_turn(line, echo=True)

    def _dispatch_command(self, line: str) -> None:
        c = self._console
        s = self.sess

        if line in ("/quit", "/q", "quit", "exit"):
            self.exit(); return
        if line == "/help":
            c.line(_HELP); return
        if line == "/clear":
            s._last_user = ""; s._last_assistant = ""
            # In place: the conversation store resolving "it"/"that one" holds this same list.
            s._session_history.clear()
            c.dim("  Started fresh: the AI no longer sees this conversation's earlier turns."); return
        if line.startswith("/rep "):
            s._rep_name = line[5:].strip()
            self.rep_name = s._rep_name
            c.dim(f"  Representative: {s._rep_name}")
            s._persist_session_state(); return
        if line.startswith("/file "):
            path = line[6:].strip()
            try:
                s._persona = open(path).read()
                s._persona_file = path
                kb = max(1, len(s._persona.encode()) // 1024)
                c.dim(f"  Loaded: {path} ({kb}KB)")
                s._persist_session_state()
            except OSError as e:
                c.dim(f"  Could not read {path!r}: {e}")
            return
        if line == "/whoami":
            s._print_whoami(); return
        if line == "/status":
            s._print_status(); return
        if line == "/tasks":
            s._print_tasks(); return
        if line in ("/models", "/model"):
            self._cmd_models_menu(); return
        if line.startswith("/model"):
            s._cmd_model(line[6:].strip()); return
        if line.startswith("/depth"):
            s._cmd_depth(line[6:].strip()); return
        if line.startswith("/system"):
            s._cmd_system(line[7:].strip()); return
        if line == "/pause":
            self._cmd_pause(); return
        if line == "/resume":
            self._cmd_resume(); return
        if line == "/replan":
            s._replan_next = True
            c.dim("  Next turn will use opus for a fresh re-planning pass."); return
        if line.startswith("/save"):
            s._cmd_save(line[5:].strip()); return
        if line.startswith("/load "):
            s._cmd_load(line[6:].strip())
            self.rep_name = s._rep_name; return
        if line == "/sessions":
            s._cmd_sessions(); return
        if line == "/reps":
            self._cmd_reps(); return
        # /quests and /goal were the old way to attach the chat to a goal; /quest does it now.
        if line in ("/quest", "/quests", "/goal"):
            self.pick_quest(); return
        if line.startswith("/goal "):
            s.cmd_quest(line[6:]); return
        if line.startswith("/quest "):
            s.cmd_quest(line[6:]); return
        c.dim(f"  Unknown command: {line!r}  (/help for list)")

    # -- interactive pickers (Textual-native) ---------------------------------

    def _ask_select(self, prompt: str, handler: Callable[[str], None]) -> None:
        """Print a prompt and route the next submitted line to ``handler``."""
        self._console.dim(prompt)
        self._pending_select = handler

    choice_picker: Optional[dict] = None

    def pick(self, title: str, options: List[tuple], handle: Callable[[str], None]) -> None:
        """Offer ``options`` ((label, description) pairs) in the menu above the prompt.

        Arrows move, Enter picks, typing narrows the list, Esc cancels. ``handle`` gets the chosen
        option's 1-based number as a string ("0" on cancel), the same contract as a typed number,
        which also still works.
        """
        self._console.dim(f"  {title}  (arrows to move, Enter to select, Esc to cancel)")
        self.choice_picker = {"options": list(options), "handle": handle}
        self._pending_select = handle
        self.refresh_command_menu()

    def close_choices(self) -> None:
        self.choice_picker = None
        self._pending_select = None
        self.refresh_command_menu()

    def _cmd_models_menu(self) -> None:
        c = self._console
        s = self.sess
        current = s._model_hint or "auto"

        def _handle(raw: str) -> None:
            try:
                n = int(raw.strip())
            except ValueError:
                c.dim("  Cancelled."); return
            if n <= 0 or n > len(s._model_tiers):
                c.dim("  Cancelled."); return
            tier_name, _ = s._model_tiers[n - 1]
            if tier_name == "auto":
                s._model_hint = None
                c.dim("  Model set to auto (orchestrator decides).")
            else:
                s._model_hint = tier_name
                c.dim(f"  Model set to {tier_name}.")
            s._persist_session_state()

        self.pick(f"Model (current: {current})",
                  [(tier, desc + ("  (current)" if tier == current else ""))
                   for tier, desc in s._model_tiers], _handle)

    def pick_quest(self, refresh: bool = False) -> None:
        """/quest with no argument: select a quest for this conversation (or auto / none).

        ``refresh`` re-fills an already open chooser in place, used when the list of quests from
        Quest arrives after the chooser opened (it is fetched in the background at startup).
        """
        from .command_menu import quest_items
        s = self.sess
        pinned = getattr(s, "pinned_quest", None)
        items = quest_items("", s.quest_choices(), getattr(s, "quest_match_mode", "auto"),
                            pinned.quest_id if pinned is not None else None)

        def handle(raw: str) -> None:
            try:
                n = int(raw.strip())
            except ValueError:
                n = 0
            if n <= 0 or n > len(items):
                self._console.dim("  Cancelled."); return
            s.cmd_quest(items[n - 1].completion[len("/quest "):])

        options = [(i.label, i.description) for i in items]
        if refresh:
            if self.choice_picker is not None and self.choice_picker.get("kind") == "quest":
                self.choice_picker.update(options=options, handle=handle)
                self._pending_select = handle
                self.refresh_command_menu()
            return
        if not s.quest_choices():
            self._console.dim("  " + s.no_quests_reason())
        self.pick("Select a quest for this conversation", options, handle)
        self.choice_picker["kind"] = "quest"

    def _cmd_reps(self) -> None:
        c = self._console
        s = self.sess
        skills_dir = s._skills_dir()
        if not skills_dir or not os.path.isdir(skills_dir):
            c.dim(f"  No SKILL.md files found in {skills_dir}")
            c.dim("  Create .claude/skills/<name>/SKILL.md, or use /rep <name> and /file <path>.")
            return
        reps: List[dict] = []
        for entry in sorted(os.scandir(skills_dir), key=lambda e: e.name):
            if not entry.is_dir():
                continue
            skill_file = os.path.join(entry.path, "SKILL.md")
            if not os.path.isfile(skill_file):
                continue
            meta = _parse_skill_frontmatter(skill_file)
            reps.append({
                "name": entry.name,
                "display_name": meta.get("display_name") or entry.name,
                "skill_file": skill_file,
            })
        if not reps:
            c.dim(f"  No SKILL.md files found under {skills_dir}."); return

        def _handle(raw: str) -> None:
            try:
                n = int(raw.strip())
            except ValueError:
                c.dim("  Cancelled."); return
            if n <= 0 or n > len(reps):
                c.dim("  Cancelled."); return
            r = reps[n - 1]
            s._rep_name = r["display_name"]
            self.rep_name = s._rep_name
            try:
                s._persona = open(r["skill_file"]).read()
                s._persona_file = r["skill_file"]
                kb = max(1, len(s._persona.encode()) // 1024)
                c.dim(f"  Representative: {s._rep_name}  (loaded {r['skill_file']}, {kb}KB)")
                s._persist_session_state()
            except OSError as e:
                c.dim(f"  Could not read {r['skill_file']!r}: {e}")

        self.pick("Representative",
                  [(r["display_name"], "(current)" if r["display_name"] == s._rep_name else "")
                   for r in reps], _handle)

    # -- "/" command menu ---------------------------------------------------------

    menu_items_shown: list = []

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if getattr(event.text_area, "id", None) == "prompt":
            self.refresh_command_menu()

    def refresh_command_menu(self) -> None:
        """Show what the "/" menu offers for the prompt's current text (hidden when nothing)."""
        from .command_menu import menu_items, parse_help
        from .interactive_session import _HELP
        try:
            menu = self.query_one("#command-menu", OptionList)
            text = self.query_one("#prompt", PromptTextArea).text
        except Exception:  # noqa: BLE001 — not mounted yet
            return
        s = self.sess
        if self.choice_picker is not None:
            from .command_menu import MenuItem
            words = [] if text.strip().isdigit() else text.lower().split()
            items = [MenuItem(label, desc, str(i), True)
                     for i, (label, desc) in enumerate(self.choice_picker["options"], 1)
                     if all(w in f"{label} {desc}".lower() for w in words)]
            self.show_menu_items(menu, items)
            return
        if not hasattr(self, "help_commands"):
            self.help_commands = parse_help(_HELP)
        pinned = getattr(s, "pinned_quest", None) if s is not None else None
        items = menu_items(
            text, self.help_commands,
            quests=(s.quest_choices() if s is not None and hasattr(s, "quest_choices") else ()),
            mode=getattr(s, "quest_match_mode", "auto") if s is not None else "auto",
            pinned_id=pinned.quest_id if pinned is not None else None,
        )
        self.show_menu_items(menu, items)

    def show_menu_items(self, menu, items) -> None:
        self.menu_items_shown = items
        menu.clear_options()
        if not items:
            menu.display = False
            return
        width = min(max(len(i.label) for i in items), 60) + 2
        menu.add_options([
            Option(Text.assemble((i.label.ljust(width), "bold"), (i.description, "dim")))
            for i in items
        ])
        menu.highlighted = 0
        menu.display = True

    def command_menu_key(self, key: str) -> bool:
        """Handle a key while the menu is open. True when the menu used it."""
        try:
            menu = self.query_one("#command-menu", OptionList)
        except Exception:  # noqa: BLE001
            return False
        if not menu.display or not self.menu_items_shown:
            return False
        count = len(self.menu_items_shown)
        current = menu.highlighted if menu.highlighted is not None else 0
        if key == "up":
            menu.highlighted = (current - 1) % count
            return True
        if key == "down":
            menu.highlighted = (current + 1) % count
            return True
        if key == "escape":
            if self.choice_picker is not None:
                handle = self.choice_picker["handle"]
                self.query_one("#prompt", PromptTextArea).text = ""
                self.close_choices()
                handle("0")
                return True
            menu.display = False
            self.menu_items_shown = []
            return True
        if key not in ("tab", "enter"):
            return False
        item = self.menu_items_shown[current]
        prompt = self.query_one("#prompt", PromptTextArea)
        if self.choice_picker is not None:
            if key == "tab":
                return True
            if prompt.text.strip().isdigit():
                return False  # a typed number is submitted as-is, the way it always worked
            handle = self.choice_picker["handle"]
            prompt.text = ""
            self.close_choices()
            self._console.dim(f"  › {item.label}")
            handle(item.completion)
            return True
        if key == "enter" and item.submit:
            # Run the highlighted command, exactly as if it had been typed out and submitted.
            prompt.post_message(PromptTextArea.Submitted(prompt, item.completion))
            menu.display = False
            self.menu_items_shown = []
            return True
        # Complete it into the prompt; a command that takes an argument keeps the menu going.
        prompt.text = item.completion
        prompt.move_cursor(prompt.document.end)
        return True
