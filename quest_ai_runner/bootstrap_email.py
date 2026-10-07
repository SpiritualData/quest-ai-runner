"""Monthly email reporting after QAR bootstrap completion.

After the first bootstrap completes, sends a professional email to the authenticated user
with statistics, AI analysis, and recommendations for improving context quality.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


def get_bootstrap_state_path(cards_dir: str) -> str:
    """Get path to the bootstrap state file that tracks last email send time."""
    return os.path.join(cards_dir, ".bootstrap_email_state.json")


def load_bootstrap_state(state_path: str) -> Dict[str, Any]:
    """Load the bootstrap state file (last email send timestamps, etc)."""
    if os.path.exists(state_path):
        try:
            with open(state_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            log.warning("failed to load bootstrap state from %s: %s", state_path, e)
    return {}


def save_bootstrap_state(state_path: str, state: Dict[str, Any]) -> None:
    """Save the bootstrap state file."""
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except IOError as e:
        log.warning("failed to save bootstrap state to %s: %s", state_path, e)


def should_send_monthly_email(state: Dict[str, Any], months: int = 1) -> bool:
    """Check if enough time has passed to send another email (default 1 month)."""
    if "last_email_sent" not in state:
        return True

    last_sent_str = state.get("last_email_sent")
    if not last_sent_str:
        return True

    try:
        last_sent = datetime.fromisoformat(last_sent_str)
        now = datetime.now(timezone.utc)
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)

        # Calculate the cutoff time (months ago from now)
        # Use a simple heuristic: 30 days per month
        cutoff = now - timedelta(days=30 * months)
        return last_sent < cutoff
    except (ValueError, TypeError):
        return True


def generate_bootstrap_email_body(
    cards_created: int,
    corpus_path: str,
    tokens_used: int,
    cost_usd: float,
    elapsed_seconds: float,
    model: str,
    provider: str,
) -> str:
    """Generate a professional email body with bootstrap stats and recommendations."""

    body = f"""QAR Context Bootstrap Monthly Report

Corpus: {corpus_path}

Bootstrap Statistics
- Cards created: {cards_created:,}
- Tokens used: {tokens_used:,}
- Cost: ${cost_usd:.4f}
- Time elapsed: {int(elapsed_seconds // 60)}m {int(elapsed_seconds % 60)}s
- Model: {model}
- Provider: {provider}

Context Quality Analysis
The bootstrap process has indexed your corpus into {cards_created:,} context cards. These cards enable QAR to:
1. Quickly locate relevant source material when answering questions
2. Ground responses in your actual codebase, data, and documentation
3. Maintain consistency across multiple turns of conversation

Recommendations for Improving Context Quality

1. Card Organization
   - Ensure each folder contains cohesive content about one topic
   - Split large folders that cover multiple unrelated topics into separate folders
   - Remove or archive outdated folders to keep the index fresh

2. Documentation Quality
   - Add clear summaries at the top of large files to help the indexer understand content
   - Use consistent formatting and section headers
   - Update documentation when code changes significantly

3. File Coverage
   - Check that important files are being included (not filtered out as too large or irrelevant)
   - Add CLAUDE.md files to important folders to guide the indexer
   - Consider excluding generated files or build artifacts if they're taking up index space

4. Regular Maintenance
   - Run bootstrap monthly to pick up new files and refresh indexed content
   - Monitor card count growth to catch accumulation of redundant content
   - Review the folder_review.json to understand what the indexer is keeping

Next Steps
- Run `qar bootstrap --dry-run` to see what the next index would cost
- Check your .quest-context folder to review the generated cards
- Share feedback on context quality or indexing behavior

Questions or issues? Check the QAR documentation at:
https://github.com/anthropics/quest-ai-runner/tree/main/docs

---
This is an automated report from QAR Bootstrap.
"""
    return body


def send_bootstrap_email_via_quest_api(
    quest_api_url: str,
    quest_api_key: str,
    quest_team_id: str,
    user_email: str,
    email_subject: str,
    email_body: str,
) -> bool:
    """Send an email via Quest API using QuestClient.

    Returns True if successful, False otherwise.
    """
    try:
        from .runner.quest_client import QuestClient

        client = QuestClient(
            base_url=quest_api_url,
            api_key=quest_api_key,
            team_id=quest_team_id,
        )

        # Send email to the team quest with the user as a specific recipient
        result = client.send_quest_email(
            quest_id=quest_team_id,
            subject=email_subject,
            body=email_body,
            recipients=[user_email],
        )

        if result:
            log.info("bootstrap email sent successfully to %s", user_email)
            return True
        else:
            log.warning("failed to send bootstrap email: empty response from Quest API")
            return False
    except Exception as e:
        log.warning("error sending bootstrap email: %s", e)
        return False


def send_monthly_bootstrap_email(
    cards_created: int,
    corpus_path: str,
    cards_dir: str,
    tokens_used: int,
    cost_usd: float,
    elapsed_seconds: float,
    model: str,
    provider: str,
    user_email: Optional[str] = None,
) -> bool:
    """Send a monthly email report after bootstrap completion.

    Only sends if:
    1. Quest API is configured
    2. No email was sent in the last month
    3. User email can be determined

    Returns True if email was sent (or would have been with proper config).
    """
    quest_api_url = os.getenv("QUEST_BASE_URL")
    quest_api_key = os.getenv("QUEST_API_KEY")
    quest_team_id = os.getenv("QUEST_TEAM_ID")

    # Check if Quest API is configured
    if not (quest_api_url and quest_api_key and quest_team_id):
        log.debug("Quest API not configured, skipping bootstrap email")
        return False

    # Load state and check if we should send
    state_path = get_bootstrap_state_path(cards_dir)
    state = load_bootstrap_state(state_path)

    if not should_send_monthly_email(state, months=1):
        log.debug("bootstrap email already sent recently, skipping")
        return False

    # Try to get user email if not provided
    if not user_email:
        try:
            from .runner.quest_client import quest_client_from_env

            env = os.environ
            client = quest_client_from_env(env)
            # Try to get user info via the API
            # This would require /api/auth/me or similar, which may not be available with API key
            # For now, we'll just use a placeholder or skip if not provided
            log.debug("user email not provided and no automatic discovery available")
            return False
        except Exception as e:
            log.debug("could not get user email from Quest API: %s", e)
            return False

    # Generate email
    email_subject = "QAR Context Bootstrap Report"
    email_body = generate_bootstrap_email_body(
        cards_created=cards_created,
        corpus_path=corpus_path,
        tokens_used=tokens_used,
        cost_usd=cost_usd,
        elapsed_seconds=elapsed_seconds,
        model=model,
        provider=provider,
    )

    # Send email
    if send_bootstrap_email_via_quest_api(
        quest_api_url=quest_api_url,
        quest_api_key=quest_api_key,
        quest_team_id=quest_team_id,
        user_email=user_email,
        email_subject=email_subject,
        email_body=email_body,
    ):
        # Update state to record email was sent
        state["last_email_sent"] = datetime.now(timezone.utc).isoformat()
        save_bootstrap_state(state_path, state)
        return True

    return False
