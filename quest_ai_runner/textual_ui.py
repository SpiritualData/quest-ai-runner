"""Textual-based interactive terminal UI for Quest AI Runner.

Replaces manual ANSI spinner with Textual's LoadingIndicator + RichLog widgets.
Provides smooth 120 FPS animation, elegant async task handling, and responsive UI.
"""
from __future__ import annotations

import asyncio
from typing import Optional, TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll, Container
from textual.widgets import LoadingIndicator, RichLog, Static, Header, Footer
from textual.binding import Binding
from textual import work

from rich.console import Console as RichConsole
from rich.markdown import Markdown as RichMarkdown
from rich.syntax import Syntax
from rich.table import Table

if TYPE_CHECKING:
    from .core.orchestrator import Orchestrator, ProgressEvent, OrchestratorResult


# ── Status widget for phase display ───────────────────────────────────────────

class StatusLine(Static):
    """Displays current phase/status (replaces spinner text)."""

    def __init__(self, text: str = "thinking…"):
        super().__init__(f"[dim]{text}[/dim]")
        self.text = text

    def update_status(self, text: str) -> None:
        """Update status text."""
        self.text = text
        self.update(f"[dim]{text}[/dim]")


# ── Context info widget ───────────────────────────────────────────────────────

class ContextInfo(Static):
    """Displays gathered sources and context cards (replaces panel)."""

    def __init__(self):
        super().__init__("")
        self.sources = []
        self.cards = []
        self.total_sources = 0
        self.replans = 0

    def add_source(self, path: str) -> None:
        """Add a source to the display."""
        if path not in self.sources:
            self.sources.append(path)
            self.total_sources += 1
        self._refresh_display()

    def set_cards(self, cards: list) -> None:
        """Set context cards."""
        self.cards = cards
        self._refresh_display()

    def inc_replans(self) -> None:
        """Increment replan count and reset display."""
        self.replans += 1
        self.sources = []
        self.cards = []
        self._refresh_display()

    def _refresh_display(self) -> None:
        """Refresh the display content."""
        lines = []

        if self.cards:
            lines.append("[bold cyan]📇 Context Cards[/bold cyan]")
            for card in self.cards:
                card_id = card.get("id", "?")
                title = card.get("title", "(no title)")[:50]
                adapter = card.get("adapter", "")
                score = card.get("relevance_score", 0)
                file_count = card.get("file_count", 0)

                adapter_label = f"[{adapter}]" if adapter else ""
                score_str = f"score: {score:.2f}" if score else "score: unknown"
                files_str = f"{file_count} file{'s' if file_count != 1 else ''}"

                lines.append(f"  [cyan]●[/cyan] {adapter_label} [dim]{card_id}[/dim]: {title}")
                lines.append(f"    [dim]{score_str}, {files_str}[/dim]")

                files = card.get("files", [])[:3]
                if files:
                    for f in files:
                        lines.append(f"      [dim]→ {f}[/dim]")
                    if len(card.get("files", [])) > 3:
                        lines.append(f"      [dim]... and {len(card.get('files', [])) - 3} more[/dim]")

        if self.sources:
            if self.cards:
                lines.append("")
            lines.append("[bold cyan]⌕ Additional Sources[/bold cyan]")
            for src in self.sources[-5:]:  # Show last 5
                prefix = "⌕" if src.startswith("(searched") else "↗"
                lines.append(f"  {prefix} {src[:70]}")
            if len(self.sources) > 5:
                lines.append(f"  [dim]and {len(self.sources) - 5} more sources...[/dim]")

        if self.total_sources > 0 or self.replans > 0:
            lines.append("")
            lines.append(f"[dim]sources: {self.total_sources}  replans: {self.replans}[/dim]")

        self.update("\n".join(lines) if lines else "")


# ── Main Textual App ──────────────────────────────────────────────────────────

class QuestAITerminal(App):
    """Textual-based terminal UI for Quest AI Runner.

    Displays:
    - Status/phase line with loading indicator
    - Context cards and sources as they're gathered
    - Accumulated output from the orchestrator
    - Real-time streaming results
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
    ]

    CSS = """
    Screen {
        background: $panel;
    }

    #status-area {
        height: auto;
        border: solid $accent;
        padding: 1 2;
    }

    #context-area {
        height: auto;
        max-height: 20;
        border: solid $accent;
        padding: 1 2;
    }

    #output-log {
        height: 1fr;
        border: solid $accent;
        padding: 1 2;
    }

    .step-line {
        color: $text;
    }

    .success {
        color: $success;
    }

    .error {
        color: $error;
    }
    """

    def __init__(self, rep_name: str = "Quest AI", **kwargs):
        super().__init__(**kwargs)
        self.rep_name = rep_name
        self.orchestrator: Optional[Orchestrator] = None

    def compose(self) -> ComposeResult:
        """Compose the UI layout."""
        yield Header()

        # Status area with loading indicator
        with Container(id="status-area"):
            yield LoadingIndicator()
            yield StatusLine()

        # Context info (sources, cards)
        yield ContextInfo(id="context-area")

        # Main output log
        yield RichLog(
            id="output-log",
            max_lines=10000,
            auto_scroll=True,
            highlight=True,
            markup=True,
        )

    def on_mount(self) -> None:
        """Initialize after UI is mounted."""
        log = self.query_one("#output-log", RichLog)
        log.write(f"[bold cyan]{self.rep_name}[/bold cyan]")

    async def run_session(self, orchestrator: Orchestrator) -> Optional[OrchestratorResult]:
        """Run an orchestrator session and stream output to the UI.

        Args:
            orchestrator: The orchestrator instance to run

        Returns:
            The orchestrator result, or None if cancelled
        """
        self.orchestrator = orchestrator

        # Run orchestrator in worker thread to avoid blocking UI
        return await self._stream_orchestrator()

    @work(thread=True, exclusive=True)
    def _stream_orchestrator(self) -> Optional[OrchestratorResult]:
        """Stream orchestrator output to UI (runs in worker thread)."""
        if not self.orchestrator:
            return None

        log = self.query_one("#output-log", RichLog)
        context = self.query_one("#context-area", ContextInfo)
        status = self.query_one("#status-area").query_one(StatusLine)

        try:
            for event in self.orchestrator.run_stream():
                self.call_from_thread(self._handle_event, event, log, context, status)
        except Exception as e:
            self.call_from_thread(log.write, f"[red]Error: {e}[/red]")
            raise

        return None

    @staticmethod
    def _handle_event(event, log: RichLog, context: ContextInfo, status: StatusLine) -> None:
        """Handle a single orchestrator event."""
        event_dict = event if isinstance(event, dict) else event.to_dict()

        event_type = event_dict.get("type", "")
        text = (event_dict.get("text") or "").rstrip()
        action = event_dict.get("action") or ""
        data = event_dict.get("data") or {}

        # Import event type constants
        from .core.adapters import (
            EVENT_CONTEXT, EVENT_STATUS, EVENT_PLAN, EVENT_READ, EVENT_REPLAN,
            EVENT_PARTIAL, EVENT_EXEC, EVENT_RESULT, EVENT_DECISION,
            EVENT_MILESTONE, EVENT_DONE,
        )

        if event_type == EVENT_STATUS:
            status_text = text or "thinking…"
            status.update_status(status_text)

        elif event_type == EVENT_PLAN:
            if text:
                log.write(f"  [dim]▸ plan[/dim]  {text[:100]}")
            status.update_status("planning…")

        elif event_type == EVENT_REPLAN:
            context.inc_replans()
            if text:
                log.write(f"  [dim]↺ replan[/dim]  {text[:100]}")
            status.update_status("re-planning…")

        elif event_type == EVENT_READ:
            paths = data.get("sources") or []
            for p in paths:
                context.add_source(p)
                prefix = "⌕" if p.startswith("(searched") else "↗"
                log.write(f"  {prefix}  {p[:70]}")

            total = context.total_sources
            status.update_status(
                f"gathering context  ({total} source{'s' if total != 1 else ''} so far)"
            )

        elif event_type == EVENT_CONTEXT:
            card_meta = data.get("card_metadata") or []
            sources = data.get("sources") or []

            if card_meta:
                context.set_cards(card_meta)
                log.write("[dim]Context cards selected:[/dim]")
                for card in card_meta:
                    card_id = card.get("id", "?")
                    title = card.get("title", "(no title)")[:60]
                    adapter = card.get("adapter", "unknown")
                    score = card.get("relevance_score", 0)
                    file_count = card.get("file_count", 0)

                    score_str = f"score: {score:.2f}" if score else "score: unknown"
                    files_str = f"{file_count} file{'s' if file_count != 1 else ''}"

                    log.write(f"  [cyan]●[/cyan] [{adapter}] [dim]{card_id}[/dim]: {title}")
                    log.write(f"    [dim]{score_str}, {files_str}[/dim]")

                    card_files = card.get("files", [])
                    if card_files:
                        for f in card_files[:5]:
                            log.write(f"      [dim]→ {f}[/dim]")
                        if len(card_files) > 5:
                            log.write(f"      [dim]... and {len(card_files) - 5} more files[/dim]")

            if sources:
                log.write("[dim]Sources:[/dim]")
                for src in sources:
                    src_adapter = src.get("adapter", "?")
                    src_label = src.get("label", src_adapter)
                    items = src.get("items") or []
                    if items:
                        items_str = ", ".join(str(x).split("/")[-1] for x in items[:3])
                        extra = f" (+{len(items) - 3} more)" if len(items) > 3 else ""
                        log.write(f"  [dim]• {src_label}: {items_str}{extra}[/dim]")

        elif event_type == EVENT_PARTIAL:
            is_ack = isinstance(data, dict) and data.get("ack")
            if is_ack and text:
                log.write(f"  [dim]✓ {text}[/dim]")
            elif text:
                # Streaming token
                log.write(text, crop=False)  # Don't crop streaming content

        elif event_type == EVENT_EXEC:
            run_id = data.get("run_id") or "default"
            goal = data.get("goal") or "executing work…"

            log.write("")
            log.write(f"[bold cyan]{goal}[/bold cyan]")

            if text:
                log.write(f"  [cyan]→[/cyan] {text[:100]}")

        elif event_type == EVENT_MILESTONE:
            if text:
                log.write(f"  [green]✓[/green] {text}")

        elif event_type == EVENT_RESULT:
            if text:
                log.write("")
                log.write("[bold cyan]Result[/bold cyan]")
                log.write(text)

        elif event_type == EVENT_DECISION:
            log.write("")
            log.write(f"  [yellow]?[/yellow] {text}")

        # EVENT_DONE is terminal signal only


if __name__ == "__main__":
    app = QuestAITerminal()
    app.run()
