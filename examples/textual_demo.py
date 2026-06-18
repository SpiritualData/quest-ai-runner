#!/usr/bin/env python3
"""Demo: Textual-based Quest AI Runner terminal.

Shows the new smooth 120 FPS terminal UI with:
- LoadingIndicator spinner (automatic animation)
- RichLog for accumulating output (auto-scroll)
- Context cards and sources display
- Real-time streaming output

Run with:
    python3 examples/textual_demo.py
"""
import asyncio
from pathlib import Path

# Add parent directory to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from quest_ai_runner.textual_ui import QuestAITerminal
from quest_ai_runner.core.orchestrator import ProgressEvent


async def demo_orchestrator_events():
    """Simulate orchestrator event stream for demo."""
    events = [
        {"type": "status", "text": "gathering context…"},
        {"type": "read", "text": "", "data": {
            "sources": ["/home/user/code/main.py", "/home/user/code/utils.py"],
            "reads": 2
        }},
        {"type": "plan", "text": "I'll analyze the code structure first", "action": "analyze"},
        {"type": "context", "text": "", "data": {
            "card_metadata": [{
                "id": "card-001",
                "title": "Code Structure Analysis",
                "adapter": "memory",
                "relevance_score": 0.92,
                "file_count": 3,
                "files": ["/home/user/code/main.py", "/home/user/code/utils.py", "/home/user/code/config.py"]
            }],
            "sources": [{
                "adapter": "files",
                "label": "Python Files",
                "items": ["/home/user/code/main.py", "/home/user/code/utils.py", "/home/user/code/config.py"]
            }]
        }},
        {"type": "status", "text": "thinking…"},
        {"type": "partial", "text": "I found ", "data": {}},
        {"type": "partial", "text": "3 key ", "data": {}},
        {"type": "partial", "text": "components: ", "data": {}},
        {"type": "partial", "text": "main, utils, ", "data": {}},
        {"type": "partial", "text": "and config.", "data": {}},
        {"type": "partial", "text": " ", "data": {"ack": True}},  # ack message
        {"type": "exec", "text": "Read: /home/user/code/main.py", "data": {
            "run_id": "exec-001",
            "goal": "Refactor main.py for clarity"
        }},
        {"type": "exec", "text": "Analyzed 150 lines", "data": {"run_id": "exec-001"}},
        {"type": "milestone", "text": "Completed initial analysis"},
        {"type": "result", "text": "## Summary\n\nThe codebase has good structure:\n- main.py: 150 lines\n- utils.py: 80 lines  \n- config.py: 45 lines\n\nRecommended improvements: add type hints, increase test coverage."},
        {"type": "done", "text": ""}
    ]

    for i, event_data in enumerate(events):
        await asyncio.sleep(0.3)  # Simulate work timing
        yield event_data


class DemoApp(QuestAITerminal):
    """Demo version that simulates orchestrator events."""

    def on_mount(self) -> None:
        """Start demo when app mounts."""
        super().on_mount()
        self._run_demo()

    def _run_demo(self) -> None:
        """Run demo event stream."""
        self.run_worker(self._demo_stream())

    async def _demo_stream(self) -> None:
        """Stream demo events to the UI."""
        log = self.query_one("#output-log")
        context = self.query_one("#context-area")
        status = self.query_one("#status-area").query_one("StatusLine")

        log.write("[dim]Starting demo... (events every 0.3 seconds)[/dim]")
        log.write("")

        async for event in demo_orchestrator_events():
            self._handle_event(event, log, context, status)
            # Let the UI update
            await asyncio.sleep(0.01)

        log.write("")
        log.write("[green]✓ Demo complete! Click Ctrl+C to exit.[/green]")


if __name__ == "__main__":
    app = DemoApp(rep_name="Demo AI")
    app.run()
