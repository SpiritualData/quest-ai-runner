"""Quest retrieval adapter — query Quest API for relevant context.

This adapter queries the Quest backend for context related to a goal/task:
- Goal metadata, notes, related goals
- Task history and results
- Cross-environment context discovery

Designed to be composed with file/db adapters via CompositeRetrievalAdapter.
Intelligently skips Quest queries when not necessary (local-only tasks).

Example:
    >>> from quest_ai_runner.adapters import CompositeRetrievalAdapter, FilesAdapter, QuestRetrievalAdapter
    >>> from quest_ai_runner.runner.quest_client import QuestClient
    >>> quest_client = QuestClient(base_url="...", api_key="...")
    >>> files = FilesAdapter("/corpus")
    >>> quest = QuestRetrievalAdapter(quest_client)
    >>> retrieval = CompositeRetrievalAdapter([files, quest])
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import Observation, RetrievalAdapter
from quest_ai_runner.runner.quest_client import QuestClient


class QuestRetrievalAdapter(RetrievalAdapter):
    """Query the Quest API for goal, task, and cross-environment context.

    This adapter:
    - Fetches goal/quest metadata and notes
    - Searches related goals and tasks
    - Supports cross-environment context discovery
    - Conditionally queries only when goal_id/quest_id is present
    - Never raises; returns Observation(kind="error") on API failures
    """

    def __init__(self, quest_client: QuestClient):
        """Initialize with a configured QuestClient.

        Args:
            quest_client: A QuestClient with base_url, api_key, and team_id set.
        """
        self.client = quest_client

    def read_section(
        self,
        rel_path: str,
        *,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        heading: Optional[str] = None,
        max_bytes: Optional[int] = None,
    ) -> Observation:
        """Not supported — Quest context is accessed via query() or discovery methods."""
        return Observation(
            kind="error",
            error="Quest adapter does not support read_section — use query() or list_sources()/describe_source()",
        )

    def grep(
        self, pattern: str, *, scope: Optional[str] = None, max_hits: Optional[int] = None
    ) -> Observation:
        """Not supported — use query() for structured lookups."""
        return Observation(
            kind="error",
            error="Quest adapter does not support grep — use query()",
        )

    def query(self, spec: Dict[str, Any]) -> Observation:
        """Structured lookup against Quest API.

        Supports:
        - goal_id + quest_id: fetch goal metadata, notes, related goals
        - task_id: fetch task details and history
        - cross_env_search: search related goals/tasks across envs

        Spec format (example):
            {
                "kind": "goal_context",
                "goal_id": "goal_123",
                "quest_id": "quest_456",
                "include_notes": True,
                "include_related": True,
            }
        """
        if not self.client.configured:
            return Observation(kind="error", error="Quest client not configured")

        kind = spec.get("kind", "")
        try:
            if kind == "goal_context":
                return self._query_goal_context(spec)
            elif kind == "task_history":
                return self._query_task_history(spec)
            elif kind == "cross_env_search":
                return self._query_cross_env(spec)
            else:
                return Observation(
                    kind="error",
                    error=f"Unknown quest query kind: {kind}. Supported: goal_context, task_history, cross_env_search",
                )
        except Exception as e:  # noqa: BLE001
            return Observation(kind="error", error=f"Quest query error: {type(e).__name__}: {e}")

    def _query_goal_context(self, spec: Dict[str, Any]) -> Observation:
        """Fetch goal metadata, notes, and related context."""
        goal_id = spec.get("goal_id")
        quest_id = spec.get("quest_id")
        include_notes = spec.get("include_notes", True)
        include_related = spec.get("include_related", False)

        if not goal_id or not quest_id:
            return Observation(
                kind="error",
                error="goal_context query requires goal_id and quest_id",
            )

        text_parts = []

        # Fetch goal metadata
        goal = self.client.get_goal(goal_id, quest_id=quest_id)
        if goal:
            text_parts.append(f"Goal: {goal.get('name', goal_id)}")
            if goal.get("description"):
                text_parts.append(f"Description: {goal['description']}")
            if goal.get("deadline"):
                text_parts.append(f"Deadline: {goal['deadline']}")
            completed = goal.get("completed")
            if completed is not None:
                text_parts.append(f"Status: {'completed' if completed else 'in progress'}")

        # Fetch recent notes
        if include_notes:
            notes = self.client.list_goal_notes(goal_id, quest_id=quest_id, limit=5)
            if notes:
                text_parts.append("\nRecent notes:")
                for note in notes:
                    text = note.get("text", "")
                    if text:
                        text_parts.append(f"  • {text}")

        # Could fetch related goals here if include_related=True
        # (requires additional API methods)

        text = "\n".join(text_parts) if text_parts else ""
        return Observation(
            kind="query",
            text=text,
            rel_path=f"quest://goal/{goal_id}",
        )

    def _query_task_history(self, spec: Dict[str, Any]) -> Observation:
        """Fetch task history and previous results (future: not yet implemented)."""
        return Observation(
            kind="error",
            error="task_history query not yet implemented",
        )

    def _query_cross_env(self, spec: Dict[str, Any]) -> Observation:
        """Search related goals/tasks across external environments (future)."""
        return Observation(
            kind="error",
            error="cross_env_search not yet implemented",
        )

    def list_sources(self) -> Observation:
        """DISCOVERY: list available Quest context sources."""
        if not self.client.configured:
            return Observation(kind="error", error="Quest client not configured")

        try:
            quests = self.client.list_quests()
            if not quests:
                return Observation(
                    kind="query",
                    text="No quests available on this Quest instance.",
                )

            lines = ["Available quests:"]
            for quest in quests[:10]:  # Limit display
                quest_id = quest.get("quest_id", "?")
                outcome = quest.get("outcome", "unknown")
                lines.append(f"  • quest/{quest_id}: {outcome}")
            if len(quests) > 10:
                lines.append(f"  ... and {len(quests) - 10} more")

            return Observation(kind="query", text="\n".join(lines))
        except Exception as e:  # noqa: BLE001
            return Observation(kind="error", error=f"list_sources failed: {e}")

    def describe_source(self, name: str, *, path: Optional[str] = None) -> Observation:
        """DISCOVERY: drill down into a specific quest or goal."""
        if not self.client.configured:
            return Observation(kind="error", error="Quest client not configured")

        # Parse name as "quest/quest_id" or "goal/goal_id"
        try:
            kind, source_id = name.split("/", 1)
        except ValueError:
            return Observation(kind="error", error=f"Invalid source name: {name}. Use 'quest/ID' or 'goal/ID'")

        if kind == "quest":
            try:
                quest = self.client.get_quest(source_id)
                if quest:
                    lines = [
                        f"Quest: {source_id}",
                        f"  Outcome: {quest.get('outcome', 'unknown')}",
                        f"  Status: {'completed' if quest.get('completed') else 'in progress'}",
                    ]
                    return Observation(kind="query", text="\n".join(lines))
                return Observation(kind="error", error=f"Quest not found: {source_id}")
            except Exception as e:  # noqa: BLE001
                return Observation(kind="error", error=f"describe_source failed: {e}")
        else:
            return Observation(kind="error", error=f"Unknown source kind: {kind}")

    def list_operations(self) -> Observation:
        """DISCOVERY: operations the brain can invoke."""
        lines = [
            "Available Quest operations:",
            "  • get_goal_context(goal_id, quest_id): fetch goal metadata + notes",
            "  • list_quests(): discover available quests",
            "  • describe_quest(quest_id): drill into a specific quest",
        ]
        return Observation(kind="query", text="\n".join(lines))

    def describe_operation(self, name: str) -> Observation:
        """DISCOVERY: full signature and usage for an operation."""
        ops = {
            "get_goal_context": "Fetch goal metadata, notes, and context. Spec: {kind: 'goal_context', goal_id: str, quest_id: str, include_notes: bool}",
            "list_quests": "List available quests on this Quest instance. Spec: {kind: 'list_quests'}",
            "describe_quest": "Drill into a specific quest. Spec: {kind: 'quest_detail', quest_id: str}",
        }
        desc = ops.get(name)
        if desc:
            return Observation(kind="query", text=desc)
        return Observation(kind="error", error=f"Unknown operation: {name}")
