"""The terminal's share of showing the AI's work: the Trail, interim updates, asks and a stopped reply.

A mixin of ``QuestAITerminal`` (see ``textual_ui.py``): methods only, state lives on the instance. The rules and the words are the hub's
(``hubofhubs.terminal``: ``render``, ``settle``, ``toggle_trail`` and the rest); this module feeds it the turn's events, draws the blocks
it returns (``textual_ui_blocks.py``), binds the keys and commands, and sends an answer to an ask through the Quest client. It decides
nothing about wording, folding or privacy; ``tests/test_terminal_uses_hub_renderers.py`` fails if it starts to.

The person at this terminal is the asker, so the work trace is requested for its owner (record titles show as the projector allows).
"""
from __future__ import annotations

import os
from typing import List, Optional

from rich.text import Text
from textual import work
from textual.widgets import Static

from hubofhubs.terminal import (
    Block, TerminalState, answer_ask, close_ask, continue_message, fold_ask, render, settle, toggle_trail, toggle_updates, unfold_ask,
)
from hubofhubs.terminal.asks import error_block, is_for_viewer

from .textual_ui_blocks import block_lines

#: Settings: ``QAR_INTERIM_REPLIES=1`` lets a long turn send "progress so far" messages (the orchestrator switch is off by default).
INTERIM_ENV = "QAR_INTERIM_REPLIES"
#: Block kinds drawn before the final answer; the rest are drawn after it.
BEFORE_ANSWER = ("earlier_updates",)


class TrailLine(Static):
    """The one live row above the activity strip: the newest page the AI has opened. Hidden when there is nothing to say."""

    def on_mount(self) -> None:
        self.display = False

    def show_text(self, text: Optional[str]) -> None:
        self.display = bool(text)
        self.update(Text(f"  {text}", style="dim") if text else "")


class TrailMixin:
    """Feeds the hub renderers and draws what they return."""

    def init_trail_state(self) -> None:
        self.term = TerminalState()
        self.turn_number = 0
        self.continue_offer = False
        self.held_ask_blocks: List[Block] = []

    # -- the turn ------------------------------------------------------------------------------------------------------------

    def term_begin_turn(self) -> None:
        """A message starts a turn: its trace, updates and outcome are fresh (an ask left open stays open)."""
        self.turn_number += 1
        self.term.new_turn(f"{self._session_id}-{self.turn_number}")
        self.continue_offer = False
        self._trail_line.show_text(None)
        self.held_ask_blocks: List[Block] = []

    def term_turn_meta(self, meta: Optional[dict]) -> dict:
        """The turn's context meta with the work trace requested for its owner (this terminal's person) and the turn's id."""
        out = dict(meta or {})
        out["work_trace"] = {"audience": "owner", "trace_id": self.term.turn_id}
        out["turn_id"] = self.term.turn_id
        cfg = getattr(getattr(self.sess, "_orch", None), "cfg", None)
        if cfg is not None and os.environ.get(INTERIM_ENV, "").strip().lower() in ("1", "true", "yes"):
            cfg.interim_replies = True
        return out

    def term_event(self, event) -> bool:
        """Give one progress event to the hub renderers and draw what they changed. True when it raised an ask for this person."""
        try:
            blocks = render(event, self.term)
        except Exception:  # noqa: BLE001 - what the AI did is decoration around the reply; it never stops a turn
            return False
        asks = [b for b in blocks if b.kind == "ask"]
        self.draw_blocks([b for b in blocks if b.kind != "ask"])
        if asks:
            # Written last, as the end of the turn (after the run's own record), like every question this terminal asks.
            self.held_ask_blocks = asks
            self._awaiting_decision = True
            self.pending_question = ""
            self._activity.set_status("Working… I'll have a question for you when this step ends.")
        return bool(asks)

    def term_closing(self) -> List[Block]:
        """The blocks that end the turn (updates folded, work summary, a stopped reason); remembers whether Continue is offered."""
        blocks = settle(self.term)
        self.continue_offer = any(a == "continue" for b in blocks for a, _ in b.actions)
        return blocks

    def draw_blocks(self, blocks: List[Block]) -> None:
        for block in blocks:
            key = self.block_key(block.fold_key)
            if block.kind == "trail_live" or (block.removed and block.fold_key == "trail" and self._turn_active):
                self._trail_line.show_text(None if block.removed else block.text)
                continue
            regions = getattr(self._tlog, "_regions", None)
            if block.removed:
                if regions is not None and key in regions:   # nothing to take away when the place was never written
                    self._tlog.put(key, [])
                continue
            lines = block_lines(block)
            if regions is None:   # a plain log (a test double): no places to rewrite, so the lines are appended
                for line in lines:
                    self._tlog.write(line)
            else:
                self._tlog.put(key, lines)

    def block_key(self, fold_key: str) -> str:
        """A turn's places are its own: an ask keeps one place for as long as it is open, everything else is per turn."""
        if fold_key.startswith("ask"):
            return fold_key
        return f"{self.term.turn_id}/{fold_key}" if fold_key else ""

    def draw_held_ask(self) -> None:
        held, self.held_ask_blocks = self.held_ask_blocks, []
        if held:
            self.write_speaker_label(force=True)
            self.draw_blocks(held)
            self.refresh_key_hints()

    # -- keys and commands ----------------------------------------------------------------------------------------------------

    def action_toggle_trail(self) -> None:
        """Alt+T: open what the AI read for the newest reply (five rows a group, then all of them), then close it."""
        blocks = toggle_trail(self.term)
        if not blocks or blocks[0].removed:
            self._console.dim("  There is nothing more to show for this reply.")
            return
        self.draw_blocks(blocks)

    def action_toggle_updates(self) -> None:
        """Alt+U: open the earlier updates of the newest reply in place, or fold them again."""
        blocks = toggle_updates(self.term)
        if not blocks:
            self._console.dim("  This reply has no earlier updates.")
            return
        self.draw_blocks(blocks)

    def action_continue_stopped(self) -> None:
        """Alt+K: carry on from a reply that stopped early, as if the person had typed the word."""
        if not self.continue_offer or self._turn_active:
            return
        self.continue_offer = False
        self.refresh_key_hints()
        self._begin_turn(continue_message(), echo=True)

    def fold_open_ask(self) -> None:
        """/notnow: the question folds to one line and stays open; the prompt is for ordinary messages again."""
        blocks = fold_ask(self.term)
        if not blocks:
            self._console.dim("  Nothing is waiting for your answer.")
            return
        self.draw_blocks(blocks)
        self.release_awaiting()

    def unfold_open_ask(self) -> None:
        """/answer: the question comes back open."""
        blocks = unfold_ask(self.term)
        if not blocks:
            self._console.dim("  Nothing is waiting for your answer.")
            return
        self.draw_blocks(blocks)
        self._awaiting_decision = True
        self.mark_prompt_awaiting(True)

    def release_awaiting(self) -> None:
        self._awaiting_decision = False
        self.mark_prompt_awaiting(False)
        self.refresh_key_hints()

    def mark_prompt_awaiting(self, awaiting: bool) -> None:
        from .textual_ui_prompt import PromptTextArea
        from .textual_ui_widgets import _AWAITING_DECISION_PLACEHOLDER, _READY_PLACEHOLDER
        box = self.query_one("#prompt", PromptTextArea)
        box.set_class(awaiting, "-awaiting")
        box.placeholder = _AWAITING_DECISION_PLACEHOLDER if awaiting else _READY_PLACEHOLDER

    def open_ask_takes(self, line: str) -> bool:
        """True when ``line`` is the answer to the open ask (an ask for this person, not folded away). It is then sent."""
        ask = self.term.ask
        if ask is None or self.term.ask_folded or self._turn_active or not is_for_viewer(ask, self.term.viewer_id):
            return False
        answer = answer_ask(self.term, line)
        if answer is None:
            self._console.dim("  Type your answer, or a number to pick an option. /notnow keeps the question open.")
            self._awaiting_decision = True
            self.mark_prompt_awaiting(True)
            return True
        self.send_answer(ask.decision_id, answer.resolution, answer.response_text)
        return True

    @work(thread=True, exclusive=False)
    def send_answer(self, decision_id: str, resolution: str, text: str) -> None:
        """Send the answer through the Quest client's resolve call (the one the app uses), off the UI thread."""
        result, failed = None, False
        try:
            client = self.sess._quest_client()
            if client is None:
                failed = True
            else:
                result = client.resolve_decision(decision_id, resolution, response_text=text)
        except Exception:  # noqa: BLE001 - the card says it could not be sent; the answer stays to try again
            failed = True
        self.call_from_thread(self.answer_sent, text, result, failed)

    def answer_sent(self, text: str, result, failed: bool) -> None:
        ask = self.term.ask
        if failed or ask is None:
            if ask is not None:
                self.draw_blocks([error_block(ask)])
            return
        follow_up = bool(result.get("follow_up_task_id")) if hasattr(result, "get") else False
        self.draw_blocks(close_ask(self.term, resumes=follow_up))
        self.release_awaiting()
        if not follow_up:
            # Nothing on Quest picks the work back up, so this session does: the answer goes on as the next message.
            self._begin_turn(text, echo=True)

    # -- the key line ---------------------------------------------------------------------------------------------------------

    def trail_key_hints(self) -> List[tuple]:
        """The keys for what the newest reply can show now: its work, its earlier updates, Continue."""
        from hubofhubs.terminal import trail as hub_trail
        hints: List[tuple] = []
        if self._turn_active:
            return hints
        if hub_trail.has_expandable_detail(self.term.steps, self.term.shared):
            hints.append(("alt+t", "What I read"))
        if any(m.kind == "interim" for m in self.term.messages):
            hints.append(("alt+u", "Earlier updates"))
        if self.continue_offer:
            hints.append(("alt+k", "Continue"))
        return hints
