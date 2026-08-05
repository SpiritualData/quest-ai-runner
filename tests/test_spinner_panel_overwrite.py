"""_ContextPanel's in-place spinner must overwrite, not duplicate, on every frame.

Regression coverage for a bug where the ANSI-fallback chat terminal (interactive.py,
used whenever the Textual UI isn't available) showed an endless stack of
"Re-planning..." lines instead of one animated line. Root cause: stdlib logging
(background adapters log at INFO from a separate feed thread, e.g. bm25_content_store's
"BM25 context index: ..." during gather/replan) wrote straight to stderr via the
default handler installed by logging.basicConfig(), with no coordination with the
panel's own cursor-position bookkeeping. A log line landing mid-spin shifted the
real terminal cursor without the panel knowing, so its next \x1b[nA moved to the
wrong row and every following frame printed as a new line forever.
"""
import io
import logging
import sys

from quest_ai_runner.interactive import _Console, _ContextPanel, _PanelAwareLogHandler


class _FakeStdout(io.StringIO):
    def isatty(self) -> bool:
        return True


def _panel(monkeypatch):
    fake = _FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake)
    console = _Console()
    console._rich = None
    console._color = False
    panel = _ContextPanel(console)
    panel._tty = True
    return panel, fake


class TestStartIsIdempotent:
    """start() must not spawn a second spin thread over an already-running one."""

    def test_start_twice_keeps_one_thread(self, monkeypatch):
        panel, _ = _panel(monkeypatch)
        panel.start()
        try:
            first_thread = panel._thread
            assert panel.is_active()
            panel.start()  # second call, no stop() in between
            assert panel._thread is first_thread, (
                "start() must no-op when a spin thread is already alive, "
                "otherwise two threads race writes to the same terminal rows"
            )
        finally:
            panel.stop()

    def test_stop_then_start_spawns_a_fresh_thread(self, monkeypatch):
        panel, _ = _panel(monkeypatch)
        panel.start()
        first_thread = panel._thread
        panel.stop()
        assert not panel.is_active()
        panel.start()
        try:
            assert panel._thread is not first_thread
        finally:
            panel.stop()


class TestRenderIsAtomic:
    """One frame must be issued as a single write() call.

    A render split across multiple write() calls can be interleaved by another
    thread's write (e.g. a log line) between them, permanently desyncing the
    cursor-up math from the real terminal state.
    """

    def test_render_single_frame_is_one_write_call(self, monkeypatch):
        panel, fake = _panel(monkeypatch)
        writes = []
        real_write = fake.write

        def counting_write(s):
            writes.append(s)
            return real_write(s)

        monkeypatch.setattr(fake, "write", counting_write)
        panel._render()
        assert len(writes) == 1, f"expected one atomic write, got {writes}"

    def test_second_frame_moves_cursor_up_before_rewriting(self, monkeypatch):
        panel, fake = _panel(monkeypatch)
        panel._render()
        panel.set_phase("Re-planning…")
        before = fake.getvalue()
        panel._render()
        after = fake.getvalue()
        new_bytes = after[len(before):]
        assert "\x1b[1A" in new_bytes, (
            "every frame after the first must move the cursor up before "
            "rewriting, or it just appends a new line"
        )

    def test_erase_is_a_single_write_call(self, monkeypatch):
        panel, fake = _panel(monkeypatch)
        panel._render()
        writes = []
        real_write = fake.write

        def counting_write(s):
            writes.append(s)
            return real_write(s)

        monkeypatch.setattr(fake, "write", counting_write)
        with panel._lock:
            panel._erase()
        assert len(writes) == 1, f"expected one atomic write, got {writes}"
        assert panel._last_line_count == 0


class TestPanelAwareLogHandler:
    """A log line during gather/replan must not corrupt the spinner."""

    def test_emit_pauses_and_resumes_an_active_panel(self, monkeypatch):
        panel, fake = _panel(monkeypatch)
        console = panel._c
        panel.start()
        try:
            assert panel.is_active()
            handler = _PanelAwareLogHandler(console, lambda: panel)
            record = logging.LogRecord(
                name="quest_ai_runner.adapters.bm25_content_store", level=logging.INFO,
                pathname=__file__, lineno=1, msg="BM25 context index: ready", args=(),
                exc_info=None,
            )
            handler.emit(record)
            # The log line must appear in the permanent scrollback text, and the
            # panel must still be spinning afterward (log lines are informational,
            # not turn-ending).
            assert "BM25 context index: ready" in fake.getvalue()
            assert panel.is_active()
        finally:
            panel.stop()

    def test_emit_does_not_resume_an_inactive_panel(self, monkeypatch):
        """A log line arriving between turns (panel already stopped) must not
        spuriously restart the spinner with nothing left to stop it later."""
        panel, fake = _panel(monkeypatch)
        console = panel._c
        assert not panel.is_active()
        handler = _PanelAwareLogHandler(console, lambda: panel)
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname=__file__, lineno=1,
            msg="late log line", args=(), exc_info=None,
        )
        handler.emit(record)
        assert "late log line" in fake.getvalue()
        assert not panel.is_active()

    def test_emit_erases_before_writing_so_lines_dont_stack(self, monkeypatch):
        """The exact reported symptom: a log line mid-spin must not leave the
        prior spinner frame on screen above the log text."""
        panel, fake = _panel(monkeypatch)
        console = panel._c
        panel.set_phase("Re-planning…")
        panel._render()  # simulate one frame already on screen, un-erased
        assert panel._last_line_count == 1
        handler = _PanelAwareLogHandler(console, lambda: panel)
        # Panel's thread isn't actually running here, so is_active() is False and
        # emit() won't call stop()/start() - drive erase() directly to prove it
        # reclaims the row instead of leaving it behind.
        with panel._lock:
            panel._erase()
        assert panel._last_line_count == 0
        console.dim("  INFO late log line")
        panel.set_phase("Re-planning…")
        panel._render()
        # Only two spinner frames plus the one log line should ever have hit the
        # stream: no leftover, un-cleared "Re-planning..." text from before erase().
        content = fake.getvalue()
        assert content.count("Re-planning…") == 2
