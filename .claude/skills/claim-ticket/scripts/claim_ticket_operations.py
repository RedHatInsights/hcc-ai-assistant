"""
Claim ticket workflow operations.

Consolidates ticket claiming into a single script:
1. get_bot_account_id - retrieve bot's JIRA account ID (cached)
2. get_transitions - get available transitions, find "In Progress"
3. assign_ticket - assign ticket to bot user
4. transition_to_in_progress - move to "In Progress" status
5. resolve_board - determine correct board from labels
6. get_active_sprint - get active sprint from board
7. add_to_sprint - add ticket to active sprint
8. task_add - track ticket in memory server

Fully integrated with JIRA Cloud API v3 and Memory Server.
"""

import argparse
import base64
import json
import logging
import os
import sys
from dataclasses import dataclass
from enum import Enum
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


class ClaimTicketOperations:
    """Handles all ticket claiming operations."""

    # Class-level cache for bot account ID
    _bot_account_id_cache: Optional[str] = None

    def __init__(
        self,
        jira_url: str,
        jira_token: str,
        jira_email: str,
        memory_url: str,
        platform_ui_board_id: int = 9297,
        default_board_id: int = 8070,
        dry_run: bool = False,
    ):
        """
        Initialize claim ticket operations handler.

        Args:
            jira_url: JIRA instance URL (e.g., https://redhat.atlassian.net)
            jira_token: JIRA API token
            jira_email: JIRA user email for Basic auth
            memory_url: Memory server base URL
            platform_ui_board_id: Board ID for platform-experience-ui tickets (default: 9297)
            default_board_id: Default board ID (default: 8070)
            dry_run: If True, log actions without executing them
        """
        self.jira_url = jira_url.rstrip("/")
        self.jira_token = jira_token
        self.jira_email = jira_email
        self.memory_url = memory_url.rstrip("/")
        self.platform_ui_board_id = platform_ui_board_id
        self.default_board_id = default_board_id
        self.dry_run = dry_run

        # Workflow state
        self.bot_account_id: Optional[str] = None
        self.transition_id: Optional[str] = None
        self.board_id: Optional[int] = None
        self.sprint_id: Optional[int] = None

    def _get_auth_header(self) -> Dict[str, str]:
        """Get Basic auth header for JIRA API."""
        auth_string = f"{self.jira_email}:{self.jira_token}"
        basic_auth = base64.b64encode(auth_string.encode()).decode()
        return {
            "Authorization": f"Basic {basic_auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def get_bot_account_id(self) -> OperationResult:
        """
        Get bot's JIRA account ID using email. Result is cached.

        Returns:
            OperationResult with account ID
        """
        # Check class-level cache first
        if ClaimTicketOperations._bot_account_id_cache:
            self.bot_account_id = ClaimTicketOperations._bot_account_id_cache
            logger.info(f"Using cached bot account ID: {self.bot_account_id}")
            return OperationResult(
                operation="get_bot_account_id",
                status=OperationStatus.SUCCESS,
                message=f"Retrieved bot account ID from cache: {self.bot_account_id}",
                details={"account_id": self.bot_account_id},
            )

        logger.info(f"Getting bot account ID for {self.jira_email}...")

        if self.dry_run:
            self.bot_account_id = "dry-run-account-id"
            logger.info(f"[DRY RUN] Would GET bot account ID for {self.jira_email}")
            return OperationResult(
                operation="get_bot_account_id",
                status=OperationStatus.SUCCESS,
                message=f"Retrieved bot account ID (dry run): {self.bot_account_id}",
                details={"account_id": self.bot_account_id},
            )

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(
                    f"{self.jira_url}/rest/api/3/user/search",
                    params={"query": self.jira_email},
                    headers=self._get_auth_header(),
                )
                response.raise_for_status()
                users = response.json()

                if not users:
                    error_msg = f"No user found for email: {self.jira_email}"
                    logger.error(error_msg)
                    return OperationResult(
                        operation="get_bot_account_id",
                        status=OperationStatus.FAILED,
                        message=error_msg,
                    )

                self.bot_account_id = users[0]["accountId"]
                # Cache the account ID
                ClaimTicketOperations._bot_account_id_cache = self.bot_account_id

                logger.info(f"Retrieved bot account ID: {self.bot_account_id}")
                return OperationResult(
                    operation="get_bot_account_id",
                    status=OperationStatus.SUCCESS,
                    message=f"Retrieved bot account ID: {self.bot_account_id}",
                    details={"account_id": self.bot_account_id},
                )
        except httpx.HTTPStatusError as e:
            error_msg = f"Failed to get bot account ID: HTTP {e.response.status_code}"
            logger.error(error_msg)
            return OperationResult(
                operation="get_bot_account_id",
                status=OperationStatus.FAILED,
                message=error_msg,
            )
        except Exception as e:
            error_msg = f"Failed to get bot account ID: {str(e)}"
            logger.error(error_msg)
            return OperationResult(
                operation="get_bot_account_id",
                status=OperationStatus.FAILED,
                message=error_msg,
            )

    def get_transitions(self, jira_key: str) -> OperationResult:
        """
        Get available transitions and find "In Progress" transition ID.

        Args:
            jira_key: JIRA ticket key (e.g., RHCLOUD-12345)

        Returns:
            OperationResult with transition ID
        """
        logger.info(f"Getting transitions for {jira_key}...")

        if self.dry_run:
            self.transition_id = "dry-run-transition-id"
            logger.info(f"[DRY RUN] Would GET transitions for {jira_key}")
            return OperationResult(
                operation="get_transitions",
                status=OperationStatus.SUCCESS,
                message=f"Found 'In Progress' transition (dry run): {self.transition_id}",
                details={"transition_id": self.transition_id},
            )

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(
                    f"{self.jira_url}/rest/api/3/issue/{jira_key}/transitions",
                    headers=self._get_auth_header(),
                )
                response.raise_for_status()
                transitions = response.json().get("transitions", [])

                # Find "In Progress" transition
                in_progress_transition = None
                for transition in transitions:
                    if (
                        transition.get("name") == "In Progress"
                        or transition.get("to", {}).get("name") == "In Progress"
                    ):
                        in_progress_transition = transition
                        break

                if not in_progress_transition:
                    available = [t.get("name", "unknown") for t in transitions]
                    error_msg = f"'In Progress' transition not found. Available: {available}"
                    logger.error(error_msg)
                    return OperationResult(
                        operation="get_transitions",
                        status=OperationStatus.FAILED,
                        message=error_msg,
                    )

                self.transition_id = in_progress_transition["id"]

                logger.info(f"Found 'In Progress' transition ID: {self.transition_id}")
                return OperationResult(
                    operation="get_transitions",
                    status=OperationStatus.SUCCESS,
                    message=f"Found 'In Progress' transition ID: {self.transition_id}",
                    details={"transition_id": self.transition_id},
                )
        except httpx.HTTPStatusError as e:
            error_msg = f"Failed to get transitions: HTTP {e.response.status_code}"
            logger.error(error_msg)
            return OperationResult(
                operation="get_transitions",
                status=OperationStatus.FAILED,
                message=error_msg,
            )
        except Exception as e:
            error_msg = f"Failed to get transitions: {str(e)}"
            logger.error(error_msg)
            return OperationResult(
                operation="get_transitions",
                status=OperationStatus.FAILED,
                message=error_msg,
            )

    def assign_ticket(self, jira_key: str) -> OperationResult:
        """
        Assign ticket to bot user.

        Args:
            jira_key: JIRA ticket key (e.g., RHCLOUD-12345)

        Returns:
            OperationResult with assignment details
        """
        if not self.bot_account_id:
            error_msg = "Bot account ID not available. Run get_bot_account_id first."
            logger.error(error_msg)
            return OperationResult(
                operation="assign_ticket",
                status=OperationStatus.FAILED,
                message=error_msg,
            )

        logger.info(f"Assigning {jira_key} to bot user {self.bot_account_id}...")

        if self.dry_run:
            logger.info(f"[DRY RUN] Would assign {jira_key} to {self.bot_account_id}")
            return OperationResult(
                operation="assign_ticket",
                status=OperationStatus.SUCCESS,
                message=f"Assigned {jira_key} to bot user (dry run)",
            )

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.put(
                    f"{self.jira_url}/rest/api/3/issue/{jira_key}/assignee",
                    headers=self._get_auth_header(),
                    json={"accountId": self.bot_account_id},
                )
                response.raise_for_status()

                logger.info(f"Successfully assigned {jira_key} to bot user")
                return OperationResult(
                    operation="assign_ticket",
                    status=OperationStatus.SUCCESS,
                    message=f"Assigned {jira_key} to bot user",
                )
        except httpx.HTTPStatusError as e:
            error_msg = f"Failed to assign ticket: HTTP {e.response.status_code}"
            logger.error(error_msg)
            return OperationResult(
                operation="assign_ticket",
                status=OperationStatus.FAILED,
                message=error_msg,
            )
        except Exception as e:
            error_msg = f"Failed to assign ticket: {str(e)}"
            logger.error(error_msg)
            return OperationResult(
                operation="assign_ticket",
                status=OperationStatus.FAILED,
                message=error_msg,
            )

    def transition_to_in_progress(self, jira_key: str) -> OperationResult:
        """
        Transition ticket to "In Progress" status.

        Args:
            jira_key: JIRA ticket key (e.g., RHCLOUD-12345)

        Returns:
            OperationResult with transition details
        """
        if not self.transition_id:
            error_msg = "Transition ID not available. Run get_transitions first."
            logger.error(error_msg)
            return OperationResult(
                operation="transition_to_in_progress",
                status=OperationStatus.FAILED,
                message=error_msg,
            )

        logger.info(f"Transitioning {jira_key} to 'In Progress'...")

        if self.dry_run:
            logger.info(f"[DRY RUN] Would transition {jira_key} to 'In Progress'")
            return OperationResult(
                operation="transition_to_in_progress",
                status=OperationStatus.SUCCESS,
                message=f"Transitioned {jira_key} to 'In Progress' (dry run)",
            )

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    f"{self.jira_url}/rest/api/3/issue/{jira_key}/transitions",
                    headers=self._get_auth_header(),
                    json={"transition": {"id": self.transition_id}},
                )
                response.raise_for_status()

                logger.info(f"Successfully transitioned {jira_key} to 'In Progress'")
                return OperationResult(
                    operation="transition_to_in_progress",
                    status=OperationStatus.SUCCESS,
                    message=f"Transitioned {jira_key} to 'In Progress'",
                )
        except httpx.HTTPStatusError as e:
            error_msg = f"Failed to transition ticket: HTTP {e.response.status_code}"
            logger.error(error_msg)
            return OperationResult(
                operation="transition_to_in_progress",
                status=OperationStatus.FAILED,
                message=error_msg,
            )
        except Exception as e:
            error_msg = f"Failed to transition ticket: {str(e)}"
            logger.error(error_msg)
            return OperationResult(
                operation="transition_to_in_progress",
                status=OperationStatus.FAILED,
                message=error_msg,
            )

    def resolve_board(self, jira_key: str) -> OperationResult:
        """
        Resolve correct board based on ticket labels.

        Logic:
        - If ticket has 'platform-experience-ui' label: use platform_ui_board_id (9297)
        - Otherwise: use default_board_id (8070)

        Args:
            jira_key: JIRA ticket key (e.g., RHCLOUD-12345)

        Returns:
            OperationResult with board ID
        """
        logger.info(f"Resolving board for {jira_key}...")

        if self.dry_run:
            self.board_id = self.default_board_id
            logger.info(f"[DRY RUN] Would resolve board for {jira_key}")
            return OperationResult(
                operation="resolve_board",
                status=OperationStatus.SUCCESS,
                message=f"Resolved board ID (dry run): {self.board_id}",
                details={"board_id": self.board_id},
            )

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(
                    f"{self.jira_url}/rest/api/3/issue/{jira_key}",
                    params={"fields": "labels"},
                    headers=self._get_auth_header(),
                )
                response.raise_for_status()
                labels = response.json().get("fields", {}).get("labels", [])

                # Check for platform-experience-ui label
                if "platform-experience-ui" in labels:
                    self.board_id = self.platform_ui_board_id
                    logger.info(f"Found 'platform-experience-ui' label, using board {self.board_id}")
                else:
                    self.board_id = self.default_board_id
                    logger.info(f"No 'platform-experience-ui' label, using default board {self.board_id}")

                return OperationResult(
                    operation="resolve_board",
                    status=OperationStatus.SUCCESS,
                    message=f"Resolved board ID: {self.board_id}",
                    details={"board_id": self.board_id, "labels": labels},
                )
        except httpx.HTTPStatusError as e:
            error_msg = f"Failed to resolve board: HTTP {e.response.status_code}"
            logger.error(error_msg)
            return OperationResult(
                operation="resolve_board",
                status=OperationStatus.FAILED,
                message=error_msg,
            )
        except Exception as e:
            error_msg = f"Failed to resolve board: {str(e)}"
            logger.error(error_msg)
            return OperationResult(
                operation="resolve_board",
                status=OperationStatus.FAILED,
                message=error_msg,
            )

    def get_active_sprint(self) -> OperationResult:
        """
        Get active sprint from the resolved board.

        Returns:
            OperationResult with sprint ID
        """
        if not self.board_id:
            error_msg = "Board ID not available. Run resolve_board first."
            logger.error(error_msg)
            return OperationResult(
                operation="get_active_sprint",
                status=OperationStatus.FAILED,
                message=error_msg,
            )

        logger.info(f"Getting active sprint from board {self.board_id}...")

        if self.dry_run:
            self.sprint_id = 12345
            logger.info(f"[DRY RUN] Would GET active sprint from board {self.board_id}")
            return OperationResult(
                operation="get_active_sprint",
                status=OperationStatus.SUCCESS,
                message=f"Found active sprint (dry run): {self.sprint_id}",
                details={"sprint_id": self.sprint_id},
            )

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(
                    f"{self.jira_url}/rest/agile/1.0/board/{self.board_id}/sprint",
                    params={"state": "active"},
                    headers=self._get_auth_header(),
                )
                response.raise_for_status()
                sprints = response.json().get("values", [])

                if not sprints:
                    error_msg = f"No active sprint found on board {self.board_id}"
                    logger.error(error_msg)
                    return OperationResult(
                        operation="get_active_sprint",
                        status=OperationStatus.FAILED,
                        message=error_msg,
                    )

                self.sprint_id = sprints[0]["id"]

                logger.info(f"Found active sprint ID: {self.sprint_id}")
                return OperationResult(
                    operation="get_active_sprint",
                    status=OperationStatus.SUCCESS,
                    message=f"Found active sprint ID: {self.sprint_id}",
                    details={"sprint_id": self.sprint_id, "sprint_name": sprints[0].get("name")},
                )
        except httpx.HTTPStatusError as e:
            error_msg = f"Failed to get active sprint: HTTP {e.response.status_code}"
            logger.error(error_msg)
            return OperationResult(
                operation="get_active_sprint",
                status=OperationStatus.FAILED,
                message=error_msg,
            )
        except Exception as e:
            error_msg = f"Failed to get active sprint: {str(e)}"
            logger.error(error_msg)
            return OperationResult(
                operation="get_active_sprint",
                status=OperationStatus.FAILED,
                message=error_msg,
            )

    def add_to_sprint(self, jira_key: str) -> OperationResult:
        """
        Add ticket to the active sprint.

        Args:
            jira_key: JIRA ticket key (e.g., RHCLOUD-12345)

        Returns:
            OperationResult with sprint addition details
        """
        if not self.sprint_id:
            error_msg = "Sprint ID not available. Run get_active_sprint first."
            logger.error(error_msg)
            return OperationResult(
                operation="add_to_sprint",
                status=OperationStatus.FAILED,
                message=error_msg,
            )

        logger.info(f"Adding {jira_key} to sprint {self.sprint_id}...")

        if self.dry_run:
            logger.info(f"[DRY RUN] Would add {jira_key} to sprint {self.sprint_id}")
            return OperationResult(
                operation="add_to_sprint",
                status=OperationStatus.SUCCESS,
                message=f"Added {jira_key} to sprint {self.sprint_id} (dry run)",
            )

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    f"{self.jira_url}/rest/agile/1.0/sprint/{self.sprint_id}/issue",
                    headers=self._get_auth_header(),
                    json={"issues": [jira_key]},
                )
                response.raise_for_status()

                logger.info(f"Successfully added {jira_key} to sprint {self.sprint_id}")
                return OperationResult(
                    operation="add_to_sprint",
                    status=OperationStatus.SUCCESS,
                    message=f"Added {jira_key} to sprint {self.sprint_id}",
                )
        except httpx.HTTPStatusError as e:
            error_msg = f"Failed to add to sprint: HTTP {e.response.status_code}"
            logger.error(error_msg)
            return OperationResult(
                operation="add_to_sprint",
                status=OperationStatus.FAILED,
                message=error_msg,
            )
        except Exception as e:
            error_msg = f"Failed to add to sprint: {str(e)}"
            logger.error(error_msg)
            return OperationResult(
                operation="add_to_sprint",
                status=OperationStatus.FAILED,
                message=error_msg,
            )

    def task_add(self, jira_key: str) -> OperationResult:
        """
        Track ticket in memory server.

        Args:
            jira_key: JIRA ticket key (e.g., RHCLOUD-12345)

        Returns:
            OperationResult with task tracking details
        """
        logger.info(f"Adding {jira_key} to memory server...")

        task_payload = {
            "jira_key": jira_key,
            "status": "in_progress",
            "assigned_to": self.bot_account_id or "unknown",
            "board_id": self.board_id,
            "sprint_id": self.sprint_id,
        }

        if self.dry_run:
            logger.info(f"[DRY RUN] Would POST to memory server: {task_payload}")
            return OperationResult(
                operation="task_add",
                status=OperationStatus.SUCCESS,
                message=f"Added {jira_key} to memory server (dry run)",
            )

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    f"{self.memory_url}/tasks",
                    json=task_payload,
                )
                response.raise_for_status()

                logger.info(f"Successfully added {jira_key} to memory server")
                return OperationResult(
                    operation="task_add",
                    status=OperationStatus.SUCCESS,
                    message=f"Added {jira_key} to memory server",
                )
        except httpx.HTTPStatusError as e:
            error_msg = f"Failed to add task to memory server: HTTP {e.response.status_code}"
            logger.error(error_msg)
            return OperationResult(
                operation="task_add",
                status=OperationStatus.FAILED,
                message=error_msg,
            )
        except Exception as e:
            error_msg = f"Failed to add task to memory server: {str(e)}"
            logger.error(error_msg)
            return OperationResult(
                operation="task_add",
                status=OperationStatus.FAILED,
                message=error_msg,
            )


def execute_claim_ticket_workflow(
    jira_key: str,
    jira_url: Optional[str] = None,
    jira_token: Optional[str] = None,
    jira_email: Optional[str] = None,
    memory_url: Optional[str] = None,
    skip_operations: Optional[List[str]] = None,
    dry_run: bool = False,
) -> WorkflowResult:
    """
    Execute the complete claim ticket workflow.

    Args:
        jira_key: JIRA ticket key (e.g., RHCLOUD-12345)
        jira_url: JIRA instance URL (optional, defaults to env var)
        jira_token: JIRA API token (optional, defaults to env var)
        jira_email: JIRA user email (optional, defaults to env var)
        memory_url: Memory server URL (optional, defaults to env var)
        skip_operations: List of operation names to skip (optional)
        dry_run: If True, log actions without executing them

    Returns:
        WorkflowResult with success status and operation results
    """
    # Resolve configuration from environment variables
    jira_url = jira_url or os.getenv("CLAIM_TICKET_JIRA_URL", "https://redhat.atlassian.net")
    jira_token = jira_token or os.getenv("CLAIM_TICKET_JIRA_TOKEN") or os.getenv("JIRA_API_TOKEN")
    jira_email = jira_email or os.getenv("CLAIM_TICKET_JIRA_EMAIL")
    memory_url = memory_url or os.getenv("BOT_MEMORY_URL")

    # Validate required configuration
    if not jira_token:
        logger.error("JIRA token not configured (set CLAIM_TICKET_JIRA_TOKEN or JIRA_API_TOKEN)")
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
        logger.error("JIRA email not configured (set CLAIM_TICKET_JIRA_EMAIL)")
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
    operations = ClaimTicketOperations(
        jira_url=jira_url,
        jira_token=jira_token,
        jira_email=jira_email,
        memory_url=memory_url,
        dry_run=dry_run,
    )

    results: List[OperationResult] = []

    # Execute operations in sequence (fail-fast)
    logger.info(f"Starting claim ticket workflow for {jira_key}...")

    # 1. Get bot account ID
    if "get_bot_account_id" not in skip_operations:
        result = operations.get_bot_account_id()
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(success=False, operations=results, jira_key=jira_key)
    else:
        results.append(
            OperationResult(
                operation="get_bot_account_id",
                status=OperationStatus.SKIPPED,
                message="Skipped by user request",
            )
        )

    # 2. Get transitions
    if "get_transitions" not in skip_operations:
        result = operations.get_transitions(jira_key)
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(success=False, operations=results, jira_key=jira_key)
    else:
        results.append(
            OperationResult(
                operation="get_transitions",
                status=OperationStatus.SKIPPED,
                message="Skipped by user request",
            )
        )

    # 3. Assign ticket
    if "assign_ticket" not in skip_operations:
        result = operations.assign_ticket(jira_key)
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(success=False, operations=results, jira_key=jira_key)
    else:
        results.append(
            OperationResult(
                operation="assign_ticket",
                status=OperationStatus.SKIPPED,
                message="Skipped by user request",
            )
        )

    # 4. Transition to In Progress
    if "transition_to_in_progress" not in skip_operations:
        result = operations.transition_to_in_progress(jira_key)
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(success=False, operations=results, jira_key=jira_key)
    else:
        results.append(
            OperationResult(
                operation="transition_to_in_progress",
                status=OperationStatus.SKIPPED,
                message="Skipped by user request",
            )
        )

    # 5. Resolve board
    if "resolve_board" not in skip_operations:
        result = operations.resolve_board(jira_key)
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(success=False, operations=results, jira_key=jira_key)
    else:
        results.append(
            OperationResult(
                operation="resolve_board",
                status=OperationStatus.SKIPPED,
                message="Skipped by user request",
            )
        )

    # 6. Get active sprint
    if "get_active_sprint" not in skip_operations:
        result = operations.get_active_sprint()
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(success=False, operations=results, jira_key=jira_key)
    else:
        results.append(
            OperationResult(
                operation="get_active_sprint",
                status=OperationStatus.SKIPPED,
                message="Skipped by user request",
            )
        )

    # 7. Add to sprint
    if "add_to_sprint" not in skip_operations:
        result = operations.add_to_sprint(jira_key)
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(success=False, operations=results, jira_key=jira_key)
    else:
        results.append(
            OperationResult(
                operation="add_to_sprint",
                status=OperationStatus.SKIPPED,
                message="Skipped by user request",
            )
        )

    # 8. Task add
    if "task_add" not in skip_operations:
        result = operations.task_add(jira_key)
        results.append(result)
        if result.status == OperationStatus.FAILED:
            return WorkflowResult(success=False, operations=results, jira_key=jira_key)
    else:
        results.append(
            OperationResult(
                operation="task_add",
                status=OperationStatus.SKIPPED,
                message="Skipped by user request",
            )
        )

    logger.info(f"Claim ticket workflow completed successfully for {jira_key}")
    return WorkflowResult(success=True, operations=results, jira_key=jira_key)


def main() -> int:
    """CLI entrypoint for claim ticket workflow."""
    parser = argparse.ArgumentParser(description="Execute claim ticket workflow")
    parser.add_argument("jira_key", help="JIRA ticket key (e.g., RHCLOUD-12345)")
    parser.add_argument("--jira-url", help="JIRA instance URL (default: env var or https://redhat.atlassian.net)")
    parser.add_argument("--jira-token", help="JIRA API token (default: env var CLAIM_TICKET_JIRA_TOKEN)")
    parser.add_argument("--jira-email", help="JIRA user email (default: env var CLAIM_TICKET_JIRA_EMAIL)")
    parser.add_argument("--memory-url", help="Memory server URL (default: env var BOT_MEMORY_URL)")
    parser.add_argument(
        "--skip",
        help="Comma-separated list of operations to skip",
        default="",
    )
    parser.add_argument("--dry-run", action="store_true", help="Log actions without executing them")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")

    args = parser.parse_args()

    skip_operations = [op.strip() for op in args.skip.split(",") if op.strip()]

    result = execute_claim_ticket_workflow(
        jira_key=args.jira_key,
        jira_url=args.jira_url,
        jira_token=args.jira_token,
        jira_email=args.jira_email,
        memory_url=args.memory_url,
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
        print(f"\nClaim ticket workflow for {result.jira_key}:")
        print("=" * 60)
        for op in result.operations:
            if op.status == OperationStatus.SUCCESS:
                status_symbol = "✓"
            elif op.status == OperationStatus.FAILED:
                status_symbol = "✗"
            else:
                status_symbol = "○"
            print(f"{status_symbol} {op.operation}: {op.message}")
        print("=" * 60)
        if result.success:
            print("✓ All operations completed successfully")
        else:
            print("✗ Workflow failed")

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
