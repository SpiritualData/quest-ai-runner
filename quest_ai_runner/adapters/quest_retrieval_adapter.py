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
from quest_ai_runner.runner.reflections import DEFAULT_PERIODS, collect_reflections


class QuestRetrievalAdapter(RetrievalAdapter):
    """Query the Quest API for goal, task, and cross-environment context.

    This adapter:
    - Fetches goal/quest metadata and notes
    - Reads the person's own reflections (daily review + period review), which are user-scoped
      and need no ids
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
        - goal_context: fetch goal metadata, notes, related goals (requires goal_id + quest_id)
        - quest_context: fetch quest metadata and goals (requires quest_id)
        - reflection_context: fetch the person's own latest daily + period reflections (no ids)
        - task_history: fetch one task in full, including the result the person read (requires
          task_id; a goal note answering an email names the task it answers)
        - cross_env_search: search related goals/tasks across envs (future)

        When exact IDs aren't available, use list_sources() first to discover quests,
        then describe_source() to drill into a specific quest.

        Spec format (example):
            {
                "kind": "goal_context",
                "goal_id": "goal_123",
                "quest_id": "quest_456",
                "include_notes": true
            }
        """
        if not self.client.configured:
            return Observation(kind="error", error="Quest client not configured")

        kind = spec.get("kind", "")
        try:
            if kind == "goal_context":
                return self._query_goal_context(spec)
            elif kind == "quest_context":
                return self._query_quest_context(spec)
            elif kind == "reflection_context":
                return self._query_reflection_context(spec)
            elif kind == "task_history":
                return self._query_task_history(spec)
            elif kind == "cross_env_search":
                return self._query_cross_env(spec)
            else:
                return Observation(
                    kind="error",
                    error=f"Unknown query kind: {kind}. Use list_sources() to discover quests first, or describe_source() to explore a specific quest.",
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
                error="goal_context query requires goal_id and quest_id. Use list_sources() to discover available quests, then describe_source('quest/<id>') to see its goals.",
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

    def _query_quest_context(self, spec: Dict[str, Any]) -> Observation:
        """Fetch quest metadata and list its goals."""
        quest_id = spec.get("quest_id")

        if not quest_id:
            return Observation(
                kind="error",
                error="quest_context query requires quest_id. Use list_sources() to discover available quests.",
            )

        text_parts = []

        # Fetch quest metadata
        quest = self.client.get_quest(quest_id)
        if quest:
            text_parts.append(f"Quest: {quest.get('name', quest_id)}")
            if quest.get("outcome"):
                text_parts.append(f"Outcome: {quest['outcome']}")
            completed = quest.get("completed")
            if completed is not None:
                text_parts.append(f"Status: {'completed' if completed else 'in progress'}")

        # Fetch goals in this quest
        goals = self.client.list_quest_goals(quest_id, limit=20)
        if goals:
            text_parts.append("\nGoals in this quest:")
            for goal in goals:
                goal_id = goal.get("goal_id", "?")
                goal_name = goal.get("name", goal_id)
                text_parts.append(f"  • {goal_name} ({goal_id})")

        text = "\n".join(text_parts) if text_parts else ""
        return Observation(
            kind="query",
            text=text,
            rel_path=f"quest://{quest_id}",
        )

    def _query_reflection_context(self, spec: Dict[str, Any]) -> Observation:
        """Fetch the person's own latest reflections: daily review plus one period review.

        Needs no goal_id or quest_id — reflections live on the USER. This exists because a planner
        asked to work "based on my daily reflection" previously had no action that could go and get
        one, so the best it could do was say it did not have the text and ask the person to paste
        it. Now it can read it.

        An absence is reported as a normal result, not an error: "the person has not written one"
        is a true, useful answer, and returning kind="error" would read as "this lookup is broken"
        and push the planner back into asking the human for text it just verified does not exist.
        """
        periods = spec.get("periods") or spec.get("period") or DEFAULT_PERIODS
        if isinstance(periods, str):
            periods = [periods]
        include_daily = spec.get("include_daily", True)
        use_previous = bool(spec.get("use_previous", False))

        ctx = collect_reflections(
            self.client,
            include_daily=bool(include_daily),
            periods=list(periods),
            use_previous=use_previous,
        )
        if not ctx.has_any():
            checked = ", ".join(ctx.checked_periods) or "no period"
            return Observation(
                kind="query",
                locator="reflection_context",
                rel_path="quest://reflections",
                text=("No reflection is recorded on Quest right now. Checked the daily plan"
                      f"{' (skipped)' if not include_daily else ''} and the {checked} review; "
                      "the person has not submitted one. Do not invent what it might have said."),
            )
        return Observation(
            kind="query",
            locator="reflection_context",
            rel_path="quest://reflections",
            text=ctx.as_text(),
        )

    # How much of a task's result to hand back. A brief is a page; anything past this is a report
    # that was written to a file, and the run asked for the message, not the archive.
    MAX_RESULT_CHARS = 20000

    def _query_task_history(self, spec: Dict[str, Any]) -> Observation:
        """One task in full: what it was asked to do and what it produced.

        This is the far end of an emailed reply. A note the person sent back by mail names the task
        whose result they were reading, and this is how a run reads that result IN FULL. Without
        it, the only thing a run has to match a reply against is the run-history block, which
        carries 300 characters of each recent result: enough to see that a brief happened, not
        enough to know what "yes, do that" is agreeing to.

        Fetched on demand rather than pasted into every prompt, because most runs never need it
        and a reply-heavy quest would otherwise carry its whole outbox in context.
        """
        task_id = spec.get("task_id")
        if not task_id:
            return Observation(
                kind="error",
                error=("task_history query requires task_id. A goal note that answers one of your "
                       "emails names it (\"task atask_...\"); pass that id here to read the "
                       "message being answered."),
            )

        task = self.client.get_task(task_id) or {}
        if not task:
            return Observation(
                kind="error",
                error=(f"No task {task_id} is readable on this Quest account. It may have been "
                       "deleted, or belong to a different account."),
            )

        parts = [f"Task: {task.get('title') or task_id}"]
        for label, key in (("Status", "status"), ("Scheduled", "scheduled_for"),
                           ("Updated", "updated_at")):
            value = task.get(key)
            if value:
                parts.append(f"{label}: {value}")
        if (task.get("text") or "").strip():
            parts.append(f"\nWhat it was asked to do:\n{task['text'].strip()}")
        result = (task.get("result") or "").strip()
        if result:
            if len(result) > self.MAX_RESULT_CHARS:
                result = result[:self.MAX_RESULT_CHARS].rstrip() + "\n\n[truncated]"
            parts.append(f"\nWhat it produced (this is what the person read):\n{result}")
        else:
            parts.append("\nThis task produced no result text.")

        return Observation(
            kind="query",
            text="\n".join(parts),
            rel_path=f"quest://task/{task_id}",
        )

    def _query_cross_env(self, spec: Dict[str, Any]) -> Observation:
        """Search related goals/tasks across external environments (future)."""
        return Observation(
            kind="error",
            error="cross_env_search not yet implemented",
        )

    def list_sources(self) -> Observation:
        """DISCOVERY: list available Quest context sources.

        Returns sources in "name: description" format for compatibility with
        CompositeRetrievalAdapter. Format: "quest/<id>: <outcome>" per line.
        """
        if not self.client.configured:
            return Observation(kind="error", error="Quest client not configured")

        try:
            quests = self.client.list_quests()
            if not quests:
                return Observation(
                    kind="error",
                    error="No quests available on this Quest instance.",
                )

            lines = []
            for quest in quests[:20]:  # Limit display to avoid explosion
                quest_id = quest.get("quest_id", "?")
                outcome = quest.get("outcome", "unknown")
                lines.append(f"quest/{quest_id}: {outcome}")

            return Observation(kind="query", text="\n".join(lines))
        except Exception as e:  # noqa: BLE001
            return Observation(kind="error", error=f"list_sources failed: {e}")

    def describe_source(self, name: str, *, path: Optional[str] = None) -> Observation:
        """DISCOVERY: drill down into a specific quest or goal.

        Supports:
        - describe_source("quest/quest_id") — shows quest metadata and its goals
        - describe_source("goal/goal_id") — shows goal details and notes
        """
        if not self.client.configured:
            return Observation(kind="error", error="Quest client not configured")

        # Parse name as "quest/quest_id" or "goal/goal_id"
        try:
            kind, source_id = name.split("/", 1)
        except ValueError:
            return Observation(kind="error", error=f"Invalid source name: {name}. Use 'quest/quest_id' or 'goal/goal_id'")

        try:
            if kind == "quest":
                quest = self.client.get_quest(source_id)
                if not quest:
                    return Observation(kind="error", error=f"Quest not found: {source_id}")

                lines = [
                    f"Quest: {quest.get('name', source_id)}",
                    f"Outcome: {quest.get('outcome', 'unknown')}",
                    f"Status: {'completed' if quest.get('completed') else 'in progress'}",
                ]

                # Include goals in this quest
                goals = self.client.list_quest_goals(source_id, limit=20)
                if goals:
                    lines.append("\nGoals:")
                    for goal in goals:
                        goal_id = goal.get("goal_id", "?")
                        goal_name = goal.get("name", goal_id)
                        lines.append(f"  • {goal_name} ({goal_id})")

                return Observation(kind="query", text="\n".join(lines))

            elif kind == "goal":
                # For goal, we need the quest_id (passed via path parameter or list_sources discovery)
                quest_id = path
                if not quest_id:
                    return Observation(
                        kind="error",
                        error=f"To describe goal {source_id}, provide its quest_id via the path parameter: describe_source('goal/{source_id}', path='quest_id')",
                    )

                goal = self.client.get_goal(source_id, quest_id=quest_id)
                if not goal:
                    return Observation(kind="error", error=f"Goal not found: {source_id}")

                lines = [
                    f"Goal: {goal.get('name', source_id)}",
                    f"Description: {goal.get('description', 'N/A')}",
                    f"Deadline: {goal.get('deadline', 'N/A')}",
                    f"Status: {'completed' if goal.get('completed') else 'in progress'}",
                ]

                return Observation(kind="query", text="\n".join(lines))

            else:
                return Observation(kind="error", error=f"Unknown source kind: {kind}. Use 'quest/ID' or 'goal/ID'")
        except Exception as e:  # noqa: BLE001
            return Observation(kind="error", error=f"describe_source failed: {e}")

    def list_operations(self) -> Observation:
        """DISCOVERY: operations the brain can invoke.

        Returns operations in "name: description" format for compatibility with
        CompositeRetrievalAdapter.
        """
        lines = [
            "get_goal_context: Fetch goal metadata and notes from Quest",
            "query_quest: Query a specific quest for goals and metadata",
            "discover_goals: List goals available in a quest",
            "get_reflection_context: Fetch the person's own latest reflections from Quest (their "
            "daily review of how yesterday went, and their week/month review). Needs no ids. Use "
            "this whenever a request refers to their reflection, their review, how their day or "
            "week went, or what they said they want to focus on",
            "get_task: Read one task in full, including the result the person received. Use it "
            "when a goal note answers one of your emails and names the task it answers",
        ]
        return Observation(kind="query", locator="list_operations", text="\n".join(lines))

    def describe_operation(self, name: str) -> Observation:
        """DISCOVERY: full signature and usage for an operation."""
        ops = {
            "get_goal_context": "Fetch goal metadata, notes, and deadline from Quest. Usage: query({kind: 'goal_context', goal_id: '...', quest_id: '...', include_notes: true})",
            "query_quest": "Query a specific quest for metadata and status. Usage: query({kind: 'goal_context', quest_id: '...'})",
            "discover_goals": "List goals within a quest. Usage: list_sources() to discover available quests, then query() with goal_id from context.",
            "get_task": (
                "Read one task in full: what it was asked to do, and the result text the person "
                "actually received. This is how you read the message an emailed reply is "
                "answering, instead of inferring it from the truncated run history. "
                "Usage: query({kind: 'task_history', task_id: 'atask_...'})"
            ),
            "get_reflection_context": (
                "Fetch what the person themselves last wrote about their own work: the daily "
                "plan's review of how the previous day went (and what they planned for the day), "
                "plus the most recent submitted period review's 'how did it go' and 'what to "
                "focus on next'. USER-scoped, so no goal_id or quest_id is needed or accepted. "
                "Usage: query({kind: 'reflection_context', periods: ['week', 'month'], "
                "include_daily: true, use_previous: false}). All fields optional; periods may be "
                "any of week/month/quarter/year, tried in the order given, first submitted review "
                "wins. When nothing is on record it returns a plain statement to that effect, not "
                "an error, so read it before telling the person you cannot see their reflection."
            ),
        }
        desc = ops.get(name)
        if desc:
            return Observation(kind="query", text=desc)
        return Observation(kind="error", error=f"Unknown operation: {name}")
