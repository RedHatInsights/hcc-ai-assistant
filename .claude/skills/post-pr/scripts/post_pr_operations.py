#!/usr/bin/env python3
"""
Post-PR workflow operations.

Consolidates post-PR-creation bookkeeping into a single script:
1. task_update - update GitHub PR (labels, JIRA link, reviewers)
2. jira_transition_issue - transition JIRA issue to "Code Review"
3. jira_add_comment - add PR link and summary as JIRA comment
4. slack_notify - send notification to Slack webhook
5. memory_store - save implementation learnings to JSON file
6. bot_status_update - update bot status to idle

Fully integrated with GitHub REST API, JIRA Cloud API v3, and Slack webhooks.
"""

import argparse
import base64
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logging.basicConfig(level=logging.INFO, format="[post-pr] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


class OperationStatus(str, Enum):
    """Operation execution status."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class OperationResult:
    """Result of a single operation."""

    operation: str
    status: OperationStatus
    message: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowResult:
    """Result of the entire post-PR workflow."""

    success: bool
    pr_url: str
    pr_number: int
    ticket_id: str
    operations: List[OperationResult]
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "success": self.success,
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
            "ticket_id": self.ticket_id,
            "timestamp": self.timestamp,
            "operations": [
                {
                    "operation": op.operation,
                    "status": op.status.value,
                    "message": op.message,
                    "timestamp": op.timestamp,
                    "details": op.details,
                }
                for op in self.operations
            ],
        }


class PostPROperations:
    """Post-PR workflow operations (stub implementations)."""

    def __init__(
        self,
        github_token: str,
        jira_url: str,
        jira_token: str,
        jira_email: str,
        slack_webhook: str,
        memory_store_path: str,
        dry_run: bool = False,
    ):
        """Initialize with configuration.

        Args:
            github_token: GitHub API token
            jira_url: JIRA instance URL (e.g., https://redhat.atlassian.net)
            jira_token: JIRA API token
            jira_email: Email for JIRA Basic auth
            slack_webhook: Slack webhook URL
            memory_store_path: Path to memory storage file
            dry_run: If True, log actions without executing
        """
        self.github_token = github_token
        self.jira_url = jira_url
        self.jira_token = jira_token
        self.jira_email = jira_email
        self.slack_webhook = slack_webhook
        self.memory_store_path = Path(memory_store_path)
        self.dry_run = dry_run

        # Ensure directories exist
        self.memory_store_path.parent.mkdir(parents=True, exist_ok=True)

    def task_update(
        self, pr_url: str, pr_number: int, ticket_id: str, reviewers: Optional[List[str]] = None
    ) -> OperationResult:
        """Update GitHub PR with labels, JIRA link in description, and request reviewers.

        Args:
            pr_url: GitHub PR URL (e.g., https://github.com/owner/repo/pull/123)
            pr_number: PR number
            ticket_id: JIRA ticket ID to link in PR description
            reviewers: List of GitHub usernames to request as reviewers

        Returns:
            OperationResult with success/failure status
        """
        try:
            if not self.github_token:
                raise ValueError(
                    "GitHub token not configured (set GITHUB_TOKEN env var or pass github_token parameter)"
                )

            # Parse owner and repo from PR URL
            # Example: https://github.com/RedHatInsights/hcc-ai-assistant/pull/123
            parts = pr_url.rstrip("/").split("/")
            if len(parts) < 5 or "github.com" not in pr_url:
                raise ValueError(f"Invalid GitHub PR URL: {pr_url}")

            owner = parts[-4]
            repo = parts[-3]

            # Default reviewers if none provided
            if reviewers is None:
                reviewers = []

            updates = []
            headers = {
                "Authorization": f"token {self.github_token}",
                "Accept": "application/vnd.github.v3+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }

            # 1. Add labels
            labels_to_add = ["code-review", "awaiting-review"]
            if self.dry_run:
                logger.info(f"[DRY RUN] Would add labels to PR #{pr_number}: {labels_to_add}")
            else:
                with httpx.Client() as client:
                    response = client.post(
                        f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/labels",
                        headers=headers,
                        json={"labels": labels_to_add},
                        timeout=30.0,
                    )
                    response.raise_for_status()
                    logger.info(f"Added labels to PR #{pr_number}: {labels_to_add}")
            updates.append(f"Added labels: {', '.join(labels_to_add)}")

            # 2. Update PR description with JIRA link
            jira_link = f"{self.jira_url}/browse/{ticket_id}"
            jira_section = f"\n\n---\n**JIRA Ticket**: [{ticket_id}]({jira_link})"

            if self.dry_run:
                logger.info(f"[DRY RUN] Would update PR description with JIRA link: {jira_link}")
            else:
                with httpx.Client() as client:
                    # First, get current PR description
                    get_response = client.get(
                        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
                        headers=headers,
                        timeout=30.0,
                    )
                    get_response.raise_for_status()
                    pr_data = get_response.json()
                    current_body = pr_data.get("body") or ""

                    # Check if JIRA link already exists
                    if ticket_id not in current_body:
                        updated_body = current_body + jira_section

                        # Update PR description
                        patch_response = client.patch(
                            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
                            headers=headers,
                            json={"body": updated_body},
                            timeout=30.0,
                        )
                        patch_response.raise_for_status()
                        logger.info(f"Updated PR description with JIRA link: {jira_link}")
                    else:
                        logger.info("JIRA link already exists in PR description")
            updates.append("Added JIRA link to description")

            # 3. Request reviewers
            if reviewers:
                if self.dry_run:
                    logger.info(f"[DRY RUN] Would request reviewers for PR #{pr_number}: {reviewers}")
                else:
                    with httpx.Client() as client:
                        response = client.post(
                            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers",
                            headers=headers,
                            json={"reviewers": reviewers},
                            timeout=30.0,
                        )
                        response.raise_for_status()
                        logger.info(f"Requested reviewers for PR #{pr_number}: {reviewers}")
                updates.append(f"Requested reviewers: {', '.join(reviewers)}")

            return OperationResult(
                operation="task_update",
                status=OperationStatus.SUCCESS,
                message=f"Updated PR #{pr_number}: {'; '.join(updates)}",
                details={
                    "pr_url": pr_url,
                    "pr_number": pr_number,
                    "owner": owner,
                    "repo": repo,
                    "labels_added": labels_to_add,
                    "jira_ticket": ticket_id,
                    "jira_link": jira_link,
                    "reviewers_requested": reviewers,
                },
            )

        except Exception as e:
            logger.error(f"Failed to update GitHub PR: {e}")
            return OperationResult(
                operation="task_update", status=OperationStatus.FAILED, message=f"GitHub PR update failed: {e}"
            )

    def jira_transition_issue(self, ticket_id: str, target_status: str = "Code Review") -> OperationResult:
        """Transition JIRA issue to target status.

        Args:
            ticket_id: JIRA ticket ID (e.g., TICKET-456)
            target_status: Target status (default: "Code Review")

        Returns:
            OperationResult with success/failure status
        """
        try:
            if not self.jira_token:
                raise ValueError("JIRA token not configured (set POST_PR_JIRA_TOKEN)")

            # Use Basic auth for JIRA Cloud (email:token)
            auth_string = f"{self.jira_email}:{self.jira_token}"
            basic_auth = base64.b64encode(auth_string.encode()).decode()
            headers = {
                "Authorization": f"Basic {basic_auth}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }

            if self.dry_run:
                logger.info(f"[DRY RUN] Would transition {ticket_id} to {target_status}")
            else:
                with httpx.Client(follow_redirects=True) as client:
                    # Get available transitions using API v3 (required for JIRA Cloud)
                    get_response = client.get(
                        f"{self.jira_url}/rest/api/3/issue/{ticket_id}/transitions",
                        headers=headers,
                        timeout=30.0,
                    )
                    get_response.raise_for_status()
                    response_data = get_response.json()
                    transitions = response_data.get("transitions", [])

                    # Find the transition ID for the target status
                    transition_id = None
                    for transition in transitions:
                        if transition.get("to", {}).get("name") == target_status:
                            transition_id = transition.get("id")
                            break

                    if not transition_id:
                        # If exact match not found, try case-insensitive match
                        for transition in transitions:
                            if transition.get("to", {}).get("name", "").lower() == target_status.lower():
                                transition_id = transition.get("id")
                                break

                    if not transition_id:
                        available = [t.get("to", {}).get("name") for t in transitions]
                        raise ValueError(
                            f"Cannot transition to '{target_status}'. Available transitions: {', '.join(available)}"
                        )

                    # Execute the transition using API v3
                    post_response = client.post(
                        f"{self.jira_url}/rest/api/3/issue/{ticket_id}/transitions",
                        headers=headers,
                        json={"transition": {"id": transition_id}},
                        timeout=30.0,
                    )
                    post_response.raise_for_status()
                    logger.info(f"Transitioned {ticket_id} to {target_status}")

            return OperationResult(
                operation="jira_transition_issue",
                status=OperationStatus.SUCCESS,
                message=f"Transitioned {ticket_id} to {target_status}",
                details={
                    "ticket_id": ticket_id,
                    "status": target_status,
                    "jira_url": f"{self.jira_url}/browse/{ticket_id}",
                },
            )

        except Exception as e:
            logger.error(f"Failed to transition JIRA issue: {e}")
            return OperationResult(
                operation="jira_transition_issue", status=OperationStatus.FAILED, message=f"JIRA transition failed: {e}"
            )

    def jira_add_comment(self, ticket_id: str, pr_url: str, summary: str) -> OperationResult:
        """Add comment to JIRA issue with PR link and summary.

        Args:
            ticket_id: JIRA ticket ID
            pr_url: GitHub PR URL
            summary: PR summary

        Returns:
            OperationResult with success/failure status
        """
        try:
            if not self.jira_token:
                raise ValueError("JIRA token not configured (set POST_PR_JIRA_TOKEN)")

            # API v3 uses Atlassian Document Format (ADF) for comments
            comment_text = f"Pull Request created: {pr_url}\n\nSummary: {summary}"
            comment_adf = {
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": f"Pull Request created: {pr_url}"}]},
                        {"type": "paragraph", "content": [{"type": "text", "text": f"Summary: {summary}"}]},
                    ],
                }
            }

            # Use Basic auth for JIRA Cloud (email:token)
            auth_string = f"{self.jira_email}:{self.jira_token}"
            basic_auth = base64.b64encode(auth_string.encode()).decode()
            headers = {
                "Authorization": f"Basic {basic_auth}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }

            if self.dry_run:
                logger.info(f"[DRY RUN] Would add comment to {ticket_id}: {comment_text}")
            else:
                with httpx.Client(follow_redirects=True) as client:
                    response = client.post(
                        f"{self.jira_url}/rest/api/3/issue/{ticket_id}/comment",
                        headers=headers,
                        json=comment_adf,
                        timeout=30.0,
                    )
                    response.raise_for_status()
                    logger.info(f"Added comment to {ticket_id}")

            return OperationResult(
                operation="jira_add_comment",
                status=OperationStatus.SUCCESS,
                message=f"Added comment to {ticket_id}",
                details={"ticket_id": ticket_id, "comment": comment_text},
            )

        except Exception as e:
            logger.error(f"Failed to add JIRA comment: {e}")
            return OperationResult(
                operation="jira_add_comment", status=OperationStatus.FAILED, message=f"JIRA comment failed: {e}"
            )

    def slack_notify(
        self, pr_url: str, pr_number: int, summary: str, channel: str = "#hcc-ai-assistant"
    ) -> OperationResult:
        """Send Slack notification for pr_created event.

        Args:
            pr_url: GitHub PR URL
            pr_number: PR number
            summary: PR summary
            channel: Slack channel (default: #hcc-ai-assistant)

        Returns:
            OperationResult with success/failure status
        """
        try:
            if not self.slack_webhook:
                raise ValueError("Slack webhook not configured (set POST_PR_SLACK_WEBHOOK)")

            message = {
                "channel": channel,
                "text": f"New PR created: #{pr_number}",
                "attachments": [
                    {
                        "color": "good",
                        "fields": [
                            {"title": "PR", "value": f"<{pr_url}|#{pr_number}>", "short": True},
                            {"title": "Summary", "value": summary, "short": False},
                        ],
                    }
                ],
            }

            if self.dry_run:
                logger.info(f"[DRY RUN] Would send Slack notification to {channel}: {message}")
            else:
                with httpx.Client() as client:
                    response = client.post(
                        self.slack_webhook,
                        json=message,
                        timeout=30.0,
                    )
                    response.raise_for_status()
                    logger.info(f"Sent Slack notification to {channel}")

            return OperationResult(
                operation="slack_notify",
                status=OperationStatus.SUCCESS,
                message=f"Sent notification to {channel}",
                details={"channel": channel, "pr_url": pr_url},
            )

        except Exception as e:
            logger.error(f"Failed to send Slack notification: {e}")
            return OperationResult(
                operation="slack_notify", status=OperationStatus.FAILED, message=f"Slack notification failed: {e}"
            )

    def memory_store(self, pr_url: str, ticket_id: str, learnings: Dict[str, Any]) -> OperationResult:
        """Store implementation learnings in memory.

        Args:
            pr_url: GitHub PR URL
            ticket_id: JIRA ticket ID
            learnings: Implementation learnings (patterns, gotchas, decisions)

        Returns:
            OperationResult with success/failure status
        """
        try:
            memory_entry = {
                "pr_url": pr_url,
                "ticket_id": ticket_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "learnings": learnings,
            }

            if self.dry_run:
                logger.info(f"[DRY RUN] Would store memory: {memory_entry}")
            else:
                # Append to JSON file (accumulative)
                memories = []
                if self.memory_store_path.exists():
                    with open(self.memory_store_path, "r") as f:
                        memories = json.load(f)
                memories.append(memory_entry)
                with open(self.memory_store_path, "w") as f:
                    json.dump(memories, f, indent=2)
                logger.info(f"Stored memory for {ticket_id}")

            return OperationResult(
                operation="memory_store",
                status=OperationStatus.SUCCESS,
                message="Stored implementation learnings",
                details=memory_entry,
            )

        except Exception as e:
            logger.error(f"Failed to store memory: {e}")
            return OperationResult(
                operation="memory_store", status=OperationStatus.FAILED, message=f"Memory storage failed: {e}"
            )

    def bot_status_update(self, status: str = "idle") -> OperationResult:
        """Update bot status.

        Args:
            status: Bot status (default: "idle")

        Returns:
            OperationResult with success/failure status
        """
        try:
            status_data = {"status": status, "timestamp": datetime.now(UTC).isoformat()}

            if self.dry_run:
                logger.info(f"[DRY RUN] Would update bot status to {status}")
            else:
                # Write to status file (overwrites previous status)
                status_file = Path("/tmp/bot_status.json")
                with open(status_file, "w") as f:
                    json.dump(status_data, f, indent=2)
                logger.info(f"Updated bot status to {status}")

            return OperationResult(
                operation="bot_status_update",
                status=OperationStatus.SUCCESS,
                message=f"Bot status set to {status}",
                details=status_data,
            )

        except Exception as e:
            logger.error(f"Failed to update bot status: {e}")
            return OperationResult(
                operation="bot_status_update", status=OperationStatus.FAILED, message=f"Bot status update failed: {e}"
            )


def execute_post_pr_workflow(
    pr_url: str,
    pr_number: int,
    ticket_id: str,
    summary: str,
    github_token: Optional[str] = None,
    jira_url: Optional[str] = None,
    jira_token: Optional[str] = None,
    jira_email: Optional[str] = None,
    slack_webhook: Optional[str] = None,
    slack_channel: str = "#hcc-ai-assistant",
    memory_store_path: Optional[str] = None,
    reviewers: Optional[List[str]] = None,
    skip_operations: Optional[List[str]] = None,
    dry_run: bool = False,
) -> WorkflowResult:
    """Execute the complete post-PR workflow.

    Args:
        pr_url: GitHub PR URL
        pr_number: PR number
        ticket_id: JIRA ticket ID
        summary: PR summary
        github_token: GitHub API token (optional, defaults to env var)
        jira_url: JIRA instance URL (optional, defaults to env var)
        jira_token: JIRA API token (optional, defaults to env var)
        jira_email: JIRA user email (optional, defaults to env var)
        slack_webhook: Slack webhook URL (optional, defaults to env var)
        slack_channel: Slack channel for notifications
        memory_store_path: Path to memory storage file (optional, defaults to env var)
        reviewers: List of GitHub usernames to request as reviewers
        skip_operations: List of operations to skip
        dry_run: Show what would be done without executing

    Returns:
        WorkflowResult with status and operation results
    """
    # Resolve configuration from environment variables
    github_token = github_token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    jira_url = jira_url or os.getenv("POST_PR_JIRA_URL", "https://redhat.atlassian.net")
    jira_token = jira_token or os.getenv("POST_PR_JIRA_TOKEN")
    jira_email = jira_email or os.getenv("POST_PR_JIRA_EMAIL", "")
    slack_webhook = slack_webhook or os.getenv("POST_PR_SLACK_WEBHOOK")
    memory_store_path = memory_store_path or os.getenv("POST_PR_MEMORY_STORE", "/tmp/memory.json")

    skip_operations = skip_operations or []
    operations = PostPROperations(
        github_token=github_token,
        jira_url=jira_url,
        jira_token=jira_token,
        jira_email=jira_email,
        slack_webhook=slack_webhook,
        memory_store_path=memory_store_path,
        dry_run=dry_run,
    )

    results: List[OperationResult] = []

    # Operation 1: Update GitHub PR (add labels, JIRA link, request reviewers)
    if "github" not in skip_operations and "task" not in skip_operations:
        result = operations.task_update(pr_url, pr_number, ticket_id, reviewers)
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(
                success=False, pr_url=pr_url, pr_number=pr_number, ticket_id=ticket_id, operations=results
            )
    else:
        results.append(
            OperationResult(operation="task_update", status=OperationStatus.SKIPPED, message="Skipped by user request")
        )

    # Operation 2: Transition JIRA issue
    if "jira" not in skip_operations:
        result = operations.jira_transition_issue(ticket_id)
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(
                success=False, pr_url=pr_url, pr_number=pr_number, ticket_id=ticket_id, operations=results
            )
    else:
        results.append(
            OperationResult(
                operation="jira_transition_issue", status=OperationStatus.SKIPPED, message="Skipped by user request"
            )
        )

    # Operation 3: Add JIRA comment
    if "jira" not in skip_operations:
        result = operations.jira_add_comment(ticket_id, pr_url, summary)
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(
                success=False, pr_url=pr_url, pr_number=pr_number, ticket_id=ticket_id, operations=results
            )
    else:
        results.append(
            OperationResult(
                operation="jira_add_comment", status=OperationStatus.SKIPPED, message="Skipped by user request"
            )
        )

    # Operation 4: Slack notification
    if "slack" not in skip_operations:
        result = operations.slack_notify(pr_url, pr_number, summary, slack_channel)
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(
                success=False, pr_url=pr_url, pr_number=pr_number, ticket_id=ticket_id, operations=results
            )
    else:
        results.append(
            OperationResult(operation="slack_notify", status=OperationStatus.SKIPPED, message="Skipped by user request")
        )

    # Operation 5: Store memory
    if "memory" not in skip_operations:
        learnings = {"summary": summary, "pr_url": pr_url, "patterns": [], "gotchas": [], "decisions": []}
        result = operations.memory_store(pr_url, ticket_id, learnings)
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(
                success=False, pr_url=pr_url, pr_number=pr_number, ticket_id=ticket_id, operations=results
            )
    else:
        results.append(
            OperationResult(operation="memory_store", status=OperationStatus.SKIPPED, message="Skipped by user request")
        )

    # Operation 6: Update bot status
    if "status" not in skip_operations:
        result = operations.bot_status_update("idle")
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(
                success=False, pr_url=pr_url, pr_number=pr_number, ticket_id=ticket_id, operations=results
            )
    else:
        results.append(
            OperationResult(
                operation="bot_status_update", status=OperationStatus.SKIPPED, message="Skipped by user request"
            )
        )

    return WorkflowResult(success=True, pr_url=pr_url, pr_number=pr_number, ticket_id=ticket_id, operations=results)


def main():
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Post-PR workflow automation")
    parser.add_argument("pr_url", help="GitHub PR URL")
    parser.add_argument("pr_number", type=int, help="PR number")
    parser.add_argument("ticket_id", help="JIRA ticket ID")
    parser.add_argument("summary", help="PR summary")
    parser.add_argument("--github-token", help="GitHub API token (falls back to GITHUB_TOKEN env var)")
    parser.add_argument("--jira-token", help="JIRA API token (falls back to POST_PR_JIRA_TOKEN env var)")
    parser.add_argument("--slack-channel", default="#hcc-ai-assistant", help="Slack channel for notifications")
    parser.add_argument("--reviewers", help="Comma-separated list of GitHub usernames to request as reviewers")
    parser.add_argument(
        "--skip", help="Comma-separated list of operations to skip (github, jira, slack, memory, status)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without executing")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of human-readable format")

    args = parser.parse_args()

    skip_operations = args.skip.split(",") if args.skip else []
    reviewers = args.reviewers.split(",") if args.reviewers else None

    result = execute_post_pr_workflow(
        pr_url=args.pr_url,
        pr_number=args.pr_number,
        ticket_id=args.ticket_id,
        summary=args.summary,
        github_token=args.github_token,
        jira_token=args.jira_token,
        slack_channel=args.slack_channel,
        reviewers=reviewers,
        skip_operations=skip_operations,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(f"\n{'=' * 80}")
        print(f"Post-PR Workflow: {'SUCCESS' if result.success else 'FAILED'}")
        print(f"{'=' * 80}")
        print(f"PR: {result.pr_url} (#{result.pr_number})")
        print(f"Ticket: {result.ticket_id}")
        print(f"Timestamp: {result.timestamp}")
        print("\nOperations:")
        for op in result.operations:
            status_icon = (
                "✓" if op.status == OperationStatus.SUCCESS else "✗" if op.status == OperationStatus.FAILED else "-"
            )
            print(f"  {status_icon} {op.operation}: {op.message}")
        print(f"{'=' * 80}\n")

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
