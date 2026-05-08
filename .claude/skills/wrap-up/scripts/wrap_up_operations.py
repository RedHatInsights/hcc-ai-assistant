"""
Wrap-up workflow operations.

Consolidates post-merge bookkeeping into a single script:
1. get_task_info - retrieve task details from memory server
2. jira_transition_issue - transition JIRA issue to "Release Pending"
3. jira_add_comment - add merge confirmation as JIRA comment
4. archive_task - archive task in memory server
5. slack_notify - send notification via memory server
6. delete_remote_branch - delete remote bot branch (GitHub/GitLab API)
7. delete_local_branch - delete local bot branch (git branch -D)
8. bot_status_update - update bot status to idle

Fully integrated with Memory Server, JIRA Cloud API v3, and GitHub/GitLab APIs.
"""

import argparse
import base64
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class OperationStatus(Enum):
    """Status of an individual operation."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class OperationResult:
    """Result of a single operation."""

    operation: str
    status: OperationStatus
    message: str
    details: Optional[Dict[str, Any]] = None


@dataclass
class WorkflowResult:
    """Result of the entire workflow."""

    success: bool
    operations: List[OperationResult]
    jira_key: str


class WrapUpOperations:
    """Handles all post-merge bookkeeping operations."""

    def __init__(
        self,
        jira_url: str,
        jira_token: str,
        jira_email: str,
        memory_url: str,
        github_token: Optional[str] = None,
        project_repos_path: str = "./project-repos.json",
        dry_run: bool = False,
    ):
        """
        Initialize wrap-up operations handler.

        Args:
            jira_url: JIRA instance URL (e.g., https://redhat.atlassian.net)
            jira_token: JIRA API token
            jira_email: JIRA user email for Basic auth
            memory_url: Memory server base URL
            github_token: GitHub API token (optional, falls back to GH_TOKEN env var)
            project_repos_path: Path to project-repos.json config file
            dry_run: If True, log actions without executing them
        """
        self.jira_url = jira_url.rstrip("/")
        self.jira_token = jira_token
        self.jira_email = jira_email
        self.memory_url = memory_url.rstrip("/")
        self.github_token = github_token or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
        self.project_repos_path = Path(project_repos_path)
        self.dry_run = dry_run

        # Task info retrieved from memory server
        self.task_info: Optional[Dict[str, Any]] = None

    def get_task_info(self, jira_key: str) -> OperationResult:
        """
        Retrieve task details from memory server.

        Args:
            jira_key: JIRA ticket key (e.g., RHCLOUD-12345)

        Returns:
            OperationResult with task information
        """
        logger.info(f"Getting task info for {jira_key} from memory server...")

        if self.dry_run:
            logger.info(f"[DRY RUN] Would GET {self.memory_url}/tasks/{jira_key}")
            self.task_info = {
                "jira_key": jira_key,
                "status": "merged",
                "pr_url": "https://github.com/example/repo/pull/123",
                "pr_number": 123,
                "repository": "example/repo",
                "branch": f"bot/{jira_key}",
                "metadata": {},
            }
            return OperationResult(
                operation="get_task_info",
                status=OperationStatus.SUCCESS,
                message="Retrieved task info (dry run)",
                details=self.task_info,
            )

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(f"{self.memory_url}/tasks/{jira_key}")
                response.raise_for_status()
                self.task_info = response.json()

                logger.info(f"Retrieved task info: {self.task_info.get('status')}")
                return OperationResult(
                    operation="get_task_info",
                    status=OperationStatus.SUCCESS,
                    message=f"Retrieved task info for {jira_key}",
                    details=self.task_info,
                )
        except httpx.HTTPStatusError as e:
            error_msg = f"Failed to get task info: HTTP {e.response.status_code}"
            logger.error(error_msg)
            return OperationResult(
                operation="get_task_info", status=OperationStatus.FAILED, message=error_msg
            )
        except Exception as e:
            error_msg = f"Failed to get task info: {str(e)}"
            logger.error(error_msg)
            return OperationResult(
                operation="get_task_info", status=OperationStatus.FAILED, message=error_msg
            )

    def jira_transition_issue(self, jira_key: str, target_status: str = "Release Pending") -> OperationResult:
        """
        Transition JIRA issue to target status using JIRA Cloud API v3.

        Args:
            jira_key: JIRA ticket key (e.g., RHCLOUD-12345)
            target_status: Target status name (default: "Release Pending")

        Returns:
            OperationResult with transition details
        """
        logger.info(f"Transitioning {jira_key} to '{target_status}'...")

        # Use Basic auth for JIRA Cloud (email:token)
        auth_string = f"{self.jira_email}:{self.jira_token}"
        basic_auth = base64.b64encode(auth_string.encode()).decode()
        headers = {
            "Authorization": f"Basic {basic_auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        if self.dry_run:
            logger.info(f"[DRY RUN] Would transition {jira_key} to '{target_status}'")
            return OperationResult(
                operation="jira_transition_issue",
                status=OperationStatus.SUCCESS,
                message=f"Transitioned {jira_key} to '{target_status}' (dry run)",
            )

        try:
            with httpx.Client(follow_redirects=True) as client:
                # Get available transitions using API v3 (required for JIRA Cloud)
                get_response = client.get(
                    f"{self.jira_url}/rest/api/3/issue/{jira_key}/transitions",
                    headers=headers,
                    timeout=30.0,
                )
                get_response.raise_for_status()
                transitions = get_response.json().get("transitions", [])

                # Find the target transition
                target_transition = None
                for transition in transitions:
                    if transition.get("name") == target_status or transition.get("to", {}).get("name") == target_status:
                        target_transition = transition
                        break

                if not target_transition:
                    available = [t.get("name", "unknown") for t in transitions]
                    error_msg = f"Transition '{target_status}' not found. Available: {available}"
                    logger.error(error_msg)
                    return OperationResult(
                        operation="jira_transition_issue",
                        status=OperationStatus.FAILED,
                        message=error_msg,
                    )

                transition_id = target_transition["id"]

                # Execute the transition
                post_response = client.post(
                    f"{self.jira_url}/rest/api/3/issue/{jira_key}/transitions",
                    headers=headers,
                    json={"transition": {"id": transition_id}},
                    timeout=30.0,
                )
                post_response.raise_for_status()

                logger.info(f"Successfully transitioned {jira_key} to '{target_status}'")
                return OperationResult(
                    operation="jira_transition_issue",
                    status=OperationStatus.SUCCESS,
                    message=f"Transitioned {jira_key} to '{target_status}'",
                )
        except httpx.HTTPStatusError as e:
            error_msg = f"Failed to transition issue: HTTP {e.response.status_code}"
            logger.error(error_msg)
            return OperationResult(
                operation="jira_transition_issue", status=OperationStatus.FAILED, message=error_msg
            )
        except Exception as e:
            error_msg = f"Failed to transition issue: {str(e)}"
            logger.error(error_msg)
            return OperationResult(
                operation="jira_transition_issue", status=OperationStatus.FAILED, message=error_msg
            )

    def jira_add_comment(self, jira_key: str) -> OperationResult:
        """
        Add merge confirmation comment to JIRA issue using ADF format.

        Args:
            jira_key: JIRA ticket key (e.g., RHCLOUD-12345)

        Returns:
            OperationResult with comment details
        """
        logger.info(f"Adding merge confirmation comment to {jira_key}...")

        if not self.task_info:
            error_msg = "Task info not available. Run get_task_info first."
            logger.error(error_msg)
            return OperationResult(
                operation="jira_add_comment", status=OperationStatus.FAILED, message=error_msg
            )

        pr_url = self.task_info.get("pr_url", "unknown")

        # API v3 uses Atlassian Document Format (ADF) for comments
        comment_adf = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": f"Pull Request merged: {pr_url}"}
                        ],
                    },
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "Changes will be deployed to stage environment in the next release."}
                        ],
                    },
                ],
            }
        }

        # Use Basic auth for JIRA Cloud
        auth_string = f"{self.jira_email}:{self.jira_token}"
        basic_auth = base64.b64encode(auth_string.encode()).decode()
        headers = {
            "Authorization": f"Basic {basic_auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        if self.dry_run:
            logger.info(f"[DRY RUN] Would add comment to {jira_key}: {comment_adf}")
            return OperationResult(
                operation="jira_add_comment",
                status=OperationStatus.SUCCESS,
                message=f"Added comment to {jira_key} (dry run)",
            )

        try:
            with httpx.Client(follow_redirects=True) as client:
                response = client.post(
                    f"{self.jira_url}/rest/api/3/issue/{jira_key}/comment",
                    headers=headers,
                    json=comment_adf,
                    timeout=30.0,
                )
                response.raise_for_status()

                logger.info(f"Successfully added comment to {jira_key}")
                return OperationResult(
                    operation="jira_add_comment",
                    status=OperationStatus.SUCCESS,
                    message=f"Added merge confirmation comment to {jira_key}",
                )
        except httpx.HTTPStatusError as e:
            error_msg = f"Failed to add comment: HTTP {e.response.status_code}"
            logger.error(error_msg)
            return OperationResult(
                operation="jira_add_comment", status=OperationStatus.FAILED, message=error_msg
            )
        except Exception as e:
            error_msg = f"Failed to add comment: {str(e)}"
            logger.error(error_msg)
            return OperationResult(
                operation="jira_add_comment", status=OperationStatus.FAILED, message=error_msg
            )

    def archive_task(self, jira_key: str) -> OperationResult:
        """
        Archive task in memory server.

        Args:
            jira_key: JIRA ticket key (e.g., RHCLOUD-12345)

        Returns:
            OperationResult with archive details
        """
        logger.info(f"Archiving task {jira_key} in memory server...")

        if self.dry_run:
            logger.info(f"[DRY RUN] Would POST {self.memory_url}/tasks/{jira_key}/archive")
            return OperationResult(
                operation="archive_task",
                status=OperationStatus.SUCCESS,
                message=f"Archived task {jira_key} (dry run)",
            )

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(f"{self.memory_url}/tasks/{jira_key}/archive")
                response.raise_for_status()

                logger.info(f"Successfully archived task {jira_key}")
                return OperationResult(
                    operation="archive_task",
                    status=OperationStatus.SUCCESS,
                    message=f"Archived task {jira_key}",
                )
        except httpx.HTTPStatusError as e:
            error_msg = f"Failed to archive task: HTTP {e.response.status_code}"
            logger.error(error_msg)
            return OperationResult(
                operation="archive_task", status=OperationStatus.FAILED, message=error_msg
            )
        except Exception as e:
            error_msg = f"Failed to archive task: {str(e)}"
            logger.error(error_msg)
            return OperationResult(
                operation="archive_task", status=OperationStatus.FAILED, message=error_msg
            )

    def slack_notify(self, jira_key: str) -> OperationResult:
        """
        Send Slack notification via memory server.

        Args:
            jira_key: JIRA ticket key (e.g., RHCLOUD-12345)

        Returns:
            OperationResult with notification details
        """
        logger.info(f"Sending Slack notification for {jira_key}...")

        if not self.task_info:
            error_msg = "Task info not available. Run get_task_info first."
            logger.error(error_msg)
            return OperationResult(
                operation="slack_notify", status=OperationStatus.FAILED, message=error_msg
            )

        notification_payload = {
            "jira_key": jira_key,
            "event": "release_pending",
            "pr_url": self.task_info.get("pr_url", "unknown"),
            "message": f"PR merged for {jira_key}, moving to Release Pending",
        }

        if self.dry_run:
            logger.info(f"[DRY RUN] Would send Slack notification: {notification_payload}")
            return OperationResult(
                operation="slack_notify",
                status=OperationStatus.SUCCESS,
                message=f"Sent Slack notification for {jira_key} (dry run)",
            )

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    f"{self.memory_url}/notifications/slack",
                    json=notification_payload,
                )
                response.raise_for_status()

                logger.info(f"Successfully sent Slack notification for {jira_key}")
                return OperationResult(
                    operation="slack_notify",
                    status=OperationStatus.SUCCESS,
                    message=f"Sent Slack notification for {jira_key}",
                )
        except httpx.HTTPStatusError as e:
            error_msg = f"Failed to send Slack notification: HTTP {e.response.status_code}"
            logger.error(error_msg)
            return OperationResult(
                operation="slack_notify", status=OperationStatus.FAILED, message=error_msg
            )
        except Exception as e:
            error_msg = f"Failed to send Slack notification: {str(e)}"
            logger.error(error_msg)
            return OperationResult(
                operation="slack_notify", status=OperationStatus.FAILED, message=error_msg
            )

    def delete_remote_branch(self) -> OperationResult:
        """
        Delete remote bot branch using GitHub API.

        Returns:
            OperationResult with branch deletion details
        """
        if not self.task_info:
            error_msg = "Task info not available. Run get_task_info first."
            logger.error(error_msg)
            return OperationResult(
                operation="delete_remote_branch", status=OperationStatus.FAILED, message=error_msg
            )

        if not self.github_token:
            error_msg = "GitHub token not configured (set GITHUB_TOKEN or GH_TOKEN)"
            logger.error(error_msg)
            return OperationResult(
                operation="delete_remote_branch", status=OperationStatus.FAILED, message=error_msg
            )

        repository = self.task_info.get("repository")
        branch = self.task_info.get("branch")

        if not repository or not branch:
            error_msg = "Repository or branch not found in task info"
            logger.error(error_msg)
            return OperationResult(
                operation="delete_remote_branch", status=OperationStatus.FAILED, message=error_msg
            )

        logger.info(f"Deleting remote branch {branch} from {repository}...")

        if self.dry_run:
            logger.info(f"[DRY RUN] Would DELETE https://api.github.com/repos/{repository}/git/refs/heads/{branch}")
            return OperationResult(
                operation="delete_remote_branch",
                status=OperationStatus.SUCCESS,
                message=f"Deleted remote branch {branch} (dry run)",
            )

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.delete(
                    f"https://api.github.com/repos/{repository}/git/refs/heads/{branch}",
                    headers={
                        "Authorization": f"Bearer {self.github_token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                response.raise_for_status()

                logger.info(f"Successfully deleted remote branch {branch}")
                return OperationResult(
                    operation="delete_remote_branch",
                    status=OperationStatus.SUCCESS,
                    message=f"Deleted remote branch {branch}",
                )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning(f"Remote branch {branch} not found (may already be deleted)")
                return OperationResult(
                    operation="delete_remote_branch",
                    status=OperationStatus.SUCCESS,
                    message=f"Remote branch {branch} not found (already deleted)",
                )
            error_msg = f"Failed to delete remote branch: HTTP {e.response.status_code}"
            logger.error(error_msg)
            return OperationResult(
                operation="delete_remote_branch", status=OperationStatus.FAILED, message=error_msg
            )
        except Exception as e:
            error_msg = f"Failed to delete remote branch: {str(e)}"
            logger.error(error_msg)
            return OperationResult(
                operation="delete_remote_branch", status=OperationStatus.FAILED, message=error_msg
            )

    def delete_local_branch(self) -> OperationResult:
        """
        Delete local bot branch using git command.

        Returns:
            OperationResult with branch deletion details
        """
        if not self.task_info:
            error_msg = "Task info not available. Run get_task_info first."
            logger.error(error_msg)
            return OperationResult(
                operation="delete_local_branch", status=OperationStatus.FAILED, message=error_msg
            )

        branch = self.task_info.get("branch")
        if not branch:
            error_msg = "Branch not found in task info"
            logger.error(error_msg)
            return OperationResult(
                operation="delete_local_branch", status=OperationStatus.FAILED, message=error_msg
            )

        logger.info(f"Deleting local branch {branch}...")

        if self.dry_run:
            logger.info(f"[DRY RUN] Would run: git branch -D {branch}")
            return OperationResult(
                operation="delete_local_branch",
                status=OperationStatus.SUCCESS,
                message=f"Deleted local branch {branch} (dry run)",
            )

        try:
            result = subprocess.run(
                ["git", "branch", "-D", branch],
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode == 0:
                logger.info(f"Successfully deleted local branch {branch}")
                return OperationResult(
                    operation="delete_local_branch",
                    status=OperationStatus.SUCCESS,
                    message=f"Deleted local branch {branch}",
                )
            elif "not found" in result.stderr.lower():
                logger.warning(f"Local branch {branch} not found (may already be deleted)")
                return OperationResult(
                    operation="delete_local_branch",
                    status=OperationStatus.SUCCESS,
                    message=f"Local branch {branch} not found (already deleted)",
                )
            else:
                error_msg = f"Failed to delete local branch: {result.stderr}"
                logger.error(error_msg)
                return OperationResult(
                    operation="delete_local_branch",
                    status=OperationStatus.FAILED,
                    message=error_msg,
                )
        except Exception as e:
            error_msg = f"Failed to delete local branch: {str(e)}"
            logger.error(error_msg)
            return OperationResult(
                operation="delete_local_branch", status=OperationStatus.FAILED, message=error_msg
            )

    def bot_status_update(self, status: str = "idle") -> OperationResult:
        """
        Update bot status in memory server.

        Args:
            status: New bot status (default: "idle")

        Returns:
            OperationResult with status update details
        """
        logger.info(f"Updating bot status to '{status}'...")

        status_payload = {"status": status}

        if self.dry_run:
            logger.info(f"[DRY RUN] Would POST {self.memory_url}/bot/status: {status_payload}")
            return OperationResult(
                operation="bot_status_update",
                status=OperationStatus.SUCCESS,
                message=f"Updated bot status to '{status}' (dry run)",
            )

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    f"{self.memory_url}/bot/status",
                    json=status_payload,
                )
                response.raise_for_status()

                logger.info(f"Successfully updated bot status to '{status}'")
                return OperationResult(
                    operation="bot_status_update",
                    status=OperationStatus.SUCCESS,
                    message=f"Updated bot status to '{status}'",
                )
        except httpx.HTTPStatusError as e:
            error_msg = f"Failed to update bot status: HTTP {e.response.status_code}"
            logger.error(error_msg)
            return OperationResult(
                operation="bot_status_update", status=OperationStatus.FAILED, message=error_msg
            )
        except Exception as e:
            error_msg = f"Failed to update bot status: {str(e)}"
            logger.error(error_msg)
            return OperationResult(
                operation="bot_status_update", status=OperationStatus.FAILED, message=error_msg
            )


def execute_wrap_up_workflow(
    jira_key: str,
    jira_url: Optional[str] = None,
    jira_token: Optional[str] = None,
    jira_email: Optional[str] = None,
    memory_url: Optional[str] = None,
    github_token: Optional[str] = None,
    skip_operations: Optional[List[str]] = None,
    dry_run: bool = False,
) -> WorkflowResult:
    """
    Execute the complete wrap-up workflow.

    Args:
        jira_key: JIRA ticket key (e.g., RHCLOUD-12345)
        jira_url: JIRA instance URL (optional, defaults to env var)
        jira_token: JIRA API token (optional, defaults to env var)
        jira_email: JIRA user email (optional, defaults to env var)
        memory_url: Memory server URL (optional, defaults to env var)
        github_token: GitHub API token (optional, defaults to env var)
        skip_operations: List of operation names to skip (optional)
        dry_run: If True, log actions without executing them

    Returns:
        WorkflowResult with success status and operation results
    """
    # Resolve configuration from environment variables
    jira_url = jira_url or os.getenv("WRAP_UP_JIRA_URL", "https://redhat.atlassian.net")
    jira_token = jira_token or os.getenv("WRAP_UP_JIRA_TOKEN")
    jira_email = jira_email or os.getenv("WRAP_UP_JIRA_EMAIL")
    memory_url = memory_url or os.getenv("BOT_MEMORY_URL")
    github_token = github_token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")

    # Validate required configuration
    if not jira_token:
        logger.error("JIRA token not configured (set WRAP_UP_JIRA_TOKEN)")
        return WorkflowResult(
            success=False,
            operations=[
                OperationResult(
                    operation="config_validation",
                    status=OperationStatus.FAILED,
                    message="JIRA token not configured",
                )
            ],
            jira_key=jira_key,
        )

    if not jira_email:
        logger.error("JIRA email not configured (set WRAP_UP_JIRA_EMAIL)")
        return WorkflowResult(
            success=False,
            operations=[
                OperationResult(
                    operation="config_validation",
                    status=OperationStatus.FAILED,
                    message="JIRA email not configured",
                )
            ],
            jira_key=jira_key,
        )

    if not memory_url:
        logger.error("Memory server URL not configured (set BOT_MEMORY_URL)")
        return WorkflowResult(
            success=False,
            operations=[
                OperationResult(
                    operation="config_validation",
                    status=OperationStatus.FAILED,
                    message="Memory server URL not configured",
                )
            ],
            jira_key=jira_key,
        )

    skip_operations = skip_operations or []
    operations = WrapUpOperations(
        jira_url=jira_url,
        jira_token=jira_token,
        jira_email=jira_email,
        memory_url=memory_url,
        github_token=github_token,
        dry_run=dry_run,
    )

    results: List[OperationResult] = []

    # Execute operations in sequence (fail-fast)
    logger.info(f"Starting wrap-up workflow for {jira_key}...")

    # 1. Get task info
    if "get_task_info" not in skip_operations:
        result = operations.get_task_info(jira_key)
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(success=False, operations=results, jira_key=jira_key)
    else:
        results.append(
            OperationResult(
                operation="get_task_info",
                status=OperationStatus.SKIPPED,
                message="Skipped by user request",
            )
        )

    # 2. Transition JIRA issue
    if "jira_transition_issue" not in skip_operations:
        result = operations.jira_transition_issue(jira_key)
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(success=False, operations=results, jira_key=jira_key)
    else:
        results.append(
            OperationResult(
                operation="jira_transition_issue",
                status=OperationStatus.SKIPPED,
                message="Skipped by user request",
            )
        )

    # 3. Add JIRA comment
    if "jira_add_comment" not in skip_operations:
        result = operations.jira_add_comment(jira_key)
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(success=False, operations=results, jira_key=jira_key)
    else:
        results.append(
            OperationResult(
                operation="jira_add_comment",
                status=OperationStatus.SKIPPED,
                message="Skipped by user request",
            )
        )

    # 4. Archive task
    if "archive_task" not in skip_operations:
        result = operations.archive_task(jira_key)
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(success=False, operations=results, jira_key=jira_key)
    else:
        results.append(
            OperationResult(
                operation="archive_task",
                status=OperationStatus.SKIPPED,
                message="Skipped by user request",
            )
        )

    # 5. Send Slack notification
    if "slack_notify" not in skip_operations:
        result = operations.slack_notify(jira_key)
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(success=False, operations=results, jira_key=jira_key)
    else:
        results.append(
            OperationResult(
                operation="slack_notify",
                status=OperationStatus.SKIPPED,
                message="Skipped by user request",
            )
        )

    # 6. Delete remote branch
    if "delete_remote_branch" not in skip_operations:
        result = operations.delete_remote_branch()
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(success=False, operations=results, jira_key=jira_key)
    else:
        results.append(
            OperationResult(
                operation="delete_remote_branch",
                status=OperationStatus.SKIPPED,
                message="Skipped by user request",
            )
        )

    # 7. Delete local branch
    if "delete_local_branch" not in skip_operations:
        result = operations.delete_local_branch()
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(success=False, operations=results, jira_key=jira_key)
    else:
        results.append(
            OperationResult(
                operation="delete_local_branch",
                status=OperationStatus.SKIPPED,
                message="Skipped by user request",
            )
        )

    # 8. Update bot status
    if "bot_status_update" not in skip_operations:
        result = operations.bot_status_update()
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(success=False, operations=results, jira_key=jira_key)
    else:
        results.append(
            OperationResult(
                operation="bot_status_update",
                status=OperationStatus.SKIPPED,
                message="Skipped by user request",
            )
        )

    logger.info(f"Wrap-up workflow completed successfully for {jira_key}")
    return WorkflowResult(success=True, operations=results, jira_key=jira_key)


def main() -> int:
    """CLI entrypoint for wrap-up workflow."""
    parser = argparse.ArgumentParser(description="Execute post-merge wrap-up workflow")
    parser.add_argument("jira_key", help="JIRA ticket key (e.g., RHCLOUD-12345)")
    parser.add_argument("--jira-url", help="JIRA instance URL (default: env var or https://redhat.atlassian.net)")
    parser.add_argument("--jira-token", help="JIRA API token (default: env var WRAP_UP_JIRA_TOKEN)")
    parser.add_argument("--jira-email", help="JIRA user email (default: env var WRAP_UP_JIRA_EMAIL)")
    parser.add_argument("--memory-url", help="Memory server URL (default: env var BOT_MEMORY_URL)")
    parser.add_argument("--github-token", help="GitHub API token (default: env var GITHUB_TOKEN or GH_TOKEN)")
    parser.add_argument(
        "--skip",
        help="Comma-separated list of operations to skip",
        default="",
    )
    parser.add_argument("--dry-run", action="store_true", help="Log actions without executing them")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")

    args = parser.parse_args()

    skip_operations = [op.strip() for op in args.skip.split(",") if op.strip()]

    result = execute_wrap_up_workflow(
        jira_key=args.jira_key,
        jira_url=args.jira_url,
        jira_token=args.jira_token,
        jira_email=args.jira_email,
        memory_url=args.memory_url,
        github_token=args.github_token,
        skip_operations=skip_operations,
        dry_run=args.dry_run,
    )

    if args.json:
        # Output JSON for programmatic consumption
        output = {
            "success": result.success,
            "jira_key": result.jira_key,
            "operations": [
                {
                    "operation": op.operation,
                    "status": op.status.value,
                    "message": op.message,
                    "details": op.details,
                }
                for op in result.operations
            ],
        }
        print(json.dumps(output, indent=2))
    else:
        # Human-readable output
        print(f"\nWrap-up workflow for {result.jira_key}:")
        print("=" * 60)
        for op in result.operations:
            status_symbol = "✓" if op.status == OperationStatus.SUCCESS else "✗" if op.status == OperationStatus.FAILED else "○"
            print(f"{status_symbol} {op.operation}: {op.message}")
        print("=" * 60)
        if result.success:
            print("✓ All operations completed successfully")
        else:
            print("✗ Workflow failed")

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
