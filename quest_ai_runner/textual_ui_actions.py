"""Key actions, clipboard copy and mouse handling for the terminal app.

A mixin of ``QuestAITerminal`` (see ``textual_ui.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import os
from typing import List, Tuple
from rich.text import Text

from .textual_ui_prompt import PromptTextArea
from .textual_ui_helpers import _copy_to_clipboard_tool


class ActionsMixin:
    """Key actions, clipboard copy and mouse handling for the terminal app."""

    # -- actions ---------------------------------------------------------------

    def _emit_osc52(self, text: str) -> bool:
        """Write an OSC-52 clipboard sequence to the terminal (best effort).

        Wrapped for tmux passthrough when ``$TMUX`` is set. Returns True if it was
        written (not proof the terminal honored it). Lets copy work over SSH / on hosts
        with no clipboard CLI, when the terminal supports OSC-52. Textual's own
        ``copy_to_clipboard`` emits plain OSC-52 with no tmux wrapping, so we do our own.
        """
        if getattr(self, "_driver", None) is None:
            return False
        try:
            import base64
            b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
            seq = f"\x1b]52;c;{b64}\x07"
            if os.environ.get("TMUX"):
                # tmux passthrough: DCS-wrap and double the inner ESC bytes.
                seq = "\x1bPtmux;" + seq.replace("\x1b", "\x1b\x1b") + "\x1b\\"
            self._driver.write(seq)
            self._clipboard = text
            return True
        except Exception:  # noqa: BLE001
            return False

    def _copy_text(self, text: str) -> tuple:
        """Copy ``text`` as robustly as possible; return ``(ok, detail)`` for feedback.

        Tries a local clipboard CLI (wl-copy/xclip/xsel) AND emits OSC-52. A local tool
        is a definite success; OSC-52 alone is best-effort (the terminal may ignore it),
        so we say so and point at the reliable fix.
        """
        tool_ok, tool_msg = _copy_to_clipboard_tool(text)
        osc_ok = self._emit_osc52(text)
        if tool_ok:
            return True, "clipboard"
        if osc_ok:
            return True, "sent to terminal (if paste fails, install wl-clipboard/xclip)"
        return False, tool_msg.strip("()")

    # -- the key line under the prompt ---------------------------------------------

    def key_hints(self) -> List[Tuple[str, str]]:
        """The keys worth showing right now, most useful first: only those that would do something.

        Esc while the AI works (or a chooser is open); Alt+D once there is work to open; Alt+C once there
        are sources to show; Tab only when there is more than one job to cycle through."""
        hints: List[Tuple[str, str]] = []
        if self._turn_active or self._pending_select is not None:
            hints.append(("esc", "Cancel"))
        runs = self._available_deep_runs()
        if runs:
            hints.append(("alt+d", "Details"))
        if self._future_ctx_panel.has_bullets:
            hints.append(("alt+c", "Sources"))
        if len(runs) > 1:
            hints.append(("tab", "Next job"))
        hints.append(("pgup/pgdn", "Scroll"))
        hints.append(("ctrl+c", "Copy / Quit"))
        return hints

    def refresh_key_hints(self) -> None:
        """Redraw the key line now (it also redraws twice a second on its own)."""
        try:
            self.query_one("#keys").refresh()
        except Exception:  # noqa: BLE001  not mounted yet
            pass

    def action_copy_last(self) -> None:
        """Copy the last AI response to the system clipboard (Alt+Y)."""
        text = "\n".join(self._answer_parts).strip() if self._answer_parts else ""
        if not text and self.sess._last_assistant:
            text = self.sess._last_assistant
        if not text:
            self.notify("Nothing to copy yet", title="Copy", timeout=2, severity="warning")
            return
        ok, detail = self._copy_text(text)
        self.notify(f"Last reply · {detail}", title="Copied" if ok else "Copy",
                    timeout=2.5, severity="information" if ok else "warning")

    def action_cancel(self) -> None:
        if self._turn_active:
            self._cancel.set()
            self._activity.set_status("Cancelling…")
        elif self._pending_select is not None:
            self._pending_select = None
            self._console.dim("  Cancelled.")

    def action_clear_log(self) -> None:
        self._tlog.clear()

    def action_quit(self) -> None:
        self.exit()

    def action_copy_or_quit(self) -> None:
        """Ctrl+C: copy the current text selection if there is one, else quit.

        Mirrors Claude Code / Textual's own convention. Because the app runs with mouse
        reporting on (see textual_session.py), a plain click-drag produces a Textual
        in-app selection (no Shift needed); this copies it to the clipboard via OSC-52
        (works locally and over SSH/mobile), clears the highlight, and stays running.
        With nothing selected, Ctrl+C quits as before. This is a priority binding so it
        fires reliably even while the prompt TextArea is focused.
        """
        try:
            selection = self.screen.get_selected_text()
        except Exception:  # noqa: BLE001
            selection = None
        if selection and selection.strip():
            self.copy_to_clipboard(selection)
            try:
                self.clear_selection()
            except Exception:  # noqa: BLE001
                pass
            preview = " ".join(selection.split())
            preview = (preview[:60] + "…") if len(preview) > 60 else preview
            self._tlog.write(Text(f"  Copied: {preview}", style="dim"))
            return
        self.exit()

    def action_scroll_up_or_agent(self) -> None:
        """PageUp: scroll transcript up; if agent detail panel is open, scroll that instead."""
        if self._deep_detail.display:
            self._deep_detail.page_back()
        else:
            self._tlog.scroll_page_up(animate=False)

    def action_scroll_down_or_agent(self) -> None:
        """PageDown: scroll transcript down; if agent detail panel is open, scroll that instead."""
        if self._deep_detail.display:
            self._deep_detail.page_forward()
        else:
            self._tlog.scroll_page_down(animate=False)

    def on_click(self, event) -> None:
        """Click anywhere to start typing: send focus back to the message input.

        The transcript is now non-focusable, but the scrollable side panels
        (deep-detail, future-context) still take focus on click. Rather than special-
        case each, we catch the bubbled Click here and refocus the prompt, so a click
        anywhere in the terminal leaves the cursor in the message box ready to type.
        Drag-to-select is unaffected: a selection left on the screen is not cleared by
        a focus change, and Ctrl+C still copies it.
        """
        try:
            prompt = self.query_one("#prompt", PromptTextArea)
        except Exception:  # noqa: BLE001
            return
        if self.focused is not prompt:
            prompt.focus()

    def on_transcript_log_clicked(self, event) -> None:
        """A plain click on the transcript (no drag) refocuses the message input."""
        try:
            self.query_one("#prompt", PromptTextArea).focus()
        except Exception:  # noqa: BLE001
            pass

    def on_transcript_log_copied(self, event) -> None:
        """Copy a drag-selection and confirm with an ephemeral toast (not scrollback)."""
        ok, detail = self._copy_text(event.text)
        n = len(event.text)
        title = "Copied" if ok else "Copy"
        self.notify(f"{n} character{'s' if n != 1 else ''} · {detail}",
                    title=title, timeout=2.5,
                    severity="information" if ok else "warning")

    def on_mouse_scroll_up(self, event) -> None:
        """Mouse wheel up — routes to the active scroll target, same logic as PageUp."""
        event.stop()
        delta = self.scroll_sensitivity_y
        if self._deep_detail.display:
            self._deep_detail._follow = False
            self._deep_detail.scroll_to(
                y=max(0, self._deep_detail.scroll_target_y - delta),
                animate=False,
            )
        else:
            self._tlog.scroll_to(
                y=max(0, self._tlog.scroll_target_y - delta),
                animate=False,
            )

    def on_mouse_scroll_down(self, event) -> None:
        """Mouse wheel down — routes to the active scroll target, same logic as PageDown."""
        event.stop()
        delta = self.scroll_sensitivity_y
        if self._deep_detail.display:
            self._deep_detail.scroll_to(
                y=self._deep_detail.scroll_target_y + delta,
                animate=False,
            )
            if self._deep_detail.scroll_y >= self._deep_detail.max_scroll_y - 1:
                self._deep_detail._follow = True
        else:
            self._tlog.scroll_to(
                y=self._tlog.scroll_target_y + delta,
                animate=False,
            )
