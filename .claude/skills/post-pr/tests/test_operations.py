"""Unit tests for post-PR operations."""

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from scripts.post_pr_operations import (
    OperationResult,
    OperationStatus,
    PostPROperations,
    WorkflowResult,
)


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def operations(temp_dir):
    """Create PostPROperations instance with temp file paths."""
    return PostPROperations(
        github_token="test-github-token",
        jira_url="https://test-jira.example.com",
        jira_token="test-jira-token",
        jira_email="test@example.com",
        slack_webhook="https://hooks.slack.com/test",
        memory_store_path=str(temp_dir / "memory.json"),
        dry_run=False,
    )


class TestTaskUpdate:
    """Test task_update operation (GitHub PR updates)."""

    @patch("scripts.post_pr_operations.httpx.Client")
    def test_task_update_success(self, mock_client_class, operations):
        """Test successful GitHub PR update."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        # Mock successful responses
        mock_post_response = Mock()
        mock_post_response.raise_for_status = Mock()

        mock_get_response = Mock()
        mock_get_response.raise_for_status = Mock()
        mock_get_response.json.return_value = {"body": "Existing PR description"}

        mock_patch_response = Mock()
        mock_patch_response.raise_for_status = Mock()

        mock_client.post.return_value = mock_post_response
        mock_client.get.return_value = mock_get_response
        mock_client.patch.return_value = mock_patch_response

        result = operations.task_update(
            pr_url="https://github.com/RedHatInsights/hcc-ai-assistant/pull/123",
            pr_number=123,
            ticket_id="TICKET-456",
            reviewers=["user1", "user2"],
        )

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "task_update"
        assert "123" in result.message
        assert result.details["pr_number"] == 123
        assert result.details["owner"] == "RedHatInsights"
        assert result.details["repo"] == "hcc-ai-assistant"
        assert result.details["jira_ticket"] == "TICKET-456"
        assert result.details["reviewers_requested"] == ["user1", "user2"]
        assert "code-review" in result.details["labels_added"]

        # Verify labels API call
        labels_call = mock_client.post.call_args_list[0]
        assert labels_call[0][0] == "https://api.github.com/repos/RedHatInsights/hcc-ai-assistant/issues/123/labels"
        assert labels_call[1]["json"]["labels"] == ["code-review", "awaiting-review"]
        assert "token test-github-token" in labels_call[1]["headers"]["Authorization"]

        # Verify GET PR call
        get_call = mock_client.get.call_args
        assert get_call[0][0] == "https://api.github.com/repos/RedHatInsights/hcc-ai-assistant/pulls/123"

        # Verify PATCH PR description call
        patch_call = mock_client.patch.call_args
        assert patch_call[0][0] == "https://api.github.com/repos/RedHatInsights/hcc-ai-assistant/pulls/123"
        assert "TICKET-456" in patch_call[1]["json"]["body"]
        assert "https://test-jira.example.com/browse/TICKET-456" in patch_call[1]["json"]["body"]

        # Verify reviewers API call
        reviewers_call = mock_client.post.call_args_list[1]
        assert (
            reviewers_call[0][0]
            == "https://api.github.com/repos/RedHatInsights/hcc-ai-assistant/pulls/123/requested_reviewers"
        )
        assert reviewers_call[1]["json"]["reviewers"] == ["user1", "user2"]

    def test_task_update_dry_run(self, temp_dir):
        """Test GitHub PR update in dry-run mode."""
        operations = PostPROperations(
            github_token="test-token",
            jira_url="https://test.atlassian.net",
            jira_token="test-jira-token",
            jira_email="test@example.com",
            slack_webhook="https://hooks.slack.com/test",
            memory_store_path=str(temp_dir / "memory.json"),
            dry_run=True,
        )

        result = operations.task_update(
            pr_url="https://github.com/test/repo/pull/2", pr_number=2, ticket_id="TICKET-789", reviewers=None
        )

        assert result.status == OperationStatus.SUCCESS
        assert result.details["owner"] == "test"
        assert result.details["repo"] == "repo"

    @patch("scripts.post_pr_operations.httpx.Client")
    def test_task_update_no_reviewers(self, mock_client_class, operations):
        """Test GitHub PR update without reviewers."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_post_response = Mock()
        mock_post_response.raise_for_status = Mock()

        mock_get_response = Mock()
        mock_get_response.raise_for_status = Mock()
        mock_get_response.json.return_value = {"body": "Existing PR description"}

        mock_patch_response = Mock()
        mock_patch_response.raise_for_status = Mock()

        mock_client.post.return_value = mock_post_response
        mock_client.get.return_value = mock_get_response
        mock_client.patch.return_value = mock_patch_response

        result = operations.task_update(
            pr_url="https://github.com/test/repo/pull/3", pr_number=3, ticket_id="TICKET-111", reviewers=None
        )

        assert result.status == OperationStatus.SUCCESS
        assert result.details["reviewers_requested"] == []

        # Verify API calls: labels + get PR + patch PR (no reviewers call)
        assert mock_client.post.call_count == 1  # labels only
        assert mock_client.get.call_count == 1  # get PR
        assert mock_client.patch.call_count == 1  # update PR

    def test_task_update_no_github_token(self, temp_dir, monkeypatch):
        """Test GitHub PR update fails without token."""
        # Clear env vars that would provide fallback tokens
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)

        operations = PostPROperations(
            github_token="",
            jira_url="https://test.atlassian.net",
            jira_token="test-jira-token",
            jira_email="test@example.com",
            slack_webhook="https://hooks.slack.com/test",
            memory_store_path=str(temp_dir / "memory.json"),
        )

        result = operations.task_update(
            pr_url="https://github.com/test/repo/pull/4", pr_number=4, ticket_id="TICKET-222", reviewers=None
        )

        assert result.status == OperationStatus.FAILED
        assert "GitHub token not configured" in result.message

    def test_task_update_invalid_url(self, operations):
        """Test GitHub PR update with invalid URL."""
        result = operations.task_update(
            pr_url="https://invalid.com/not/a/pr", pr_number=5, ticket_id="TICKET-333", reviewers=None
        )

        assert result.status == OperationStatus.FAILED
        assert "Invalid GitHub PR URL" in result.message


class TestJiraTransitionIssue:
    """Test jira_transition_issue operation."""

    @patch("scripts.post_pr_operations.httpx.Client")
    def test_jira_transition_success(self, mock_client_class, operations):
        """Test successful JIRA transition."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        # Mock GET transitions response
        mock_get_response = Mock()
        mock_get_response.raise_for_status = Mock()
        mock_get_response.json.return_value = {
            "transitions": [
                {"id": "11", "to": {"name": "In Progress"}},
                {"id": "21", "to": {"name": "Code Review"}},
                {"id": "31", "to": {"name": "Done"}},
            ]
        }

        # Mock POST transition response
        mock_post_response = Mock()
        mock_post_response.raise_for_status = Mock()

        mock_client.get.return_value = mock_get_response
        mock_client.post.return_value = mock_post_response

        result = operations.jira_transition_issue(ticket_id="TICKET-123", target_status="Code Review")

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "jira_transition_issue"
        assert "TICKET-123" in result.message
        assert result.details["status"] == "Code Review"
        assert "jira_url" in result.details

        # Verify GET request to fetch available transitions (API v3 with Basic auth)
        mock_client.get.assert_called_once_with(
            "https://test-jira.example.com/rest/api/3/issue/TICKET-123/transitions",
            headers={
                "Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTp0ZXN0LWppcmEtdG9rZW4=",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

        # Verify POST request to execute the transition with correct transition ID
        mock_client.post.assert_called_once_with(
            "https://test-jira.example.com/rest/api/3/issue/TICKET-123/transitions",
            headers={
                "Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTp0ZXN0LWppcmEtdG9rZW4=",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"transition": {"id": "21"}},  # ID for "Code Review"
            timeout=30.0,
        )

    def test_jira_transition_no_token(self, temp_dir, monkeypatch):
        """Test JIRA transition fails without token."""
        monkeypatch.delenv("POST_PR_JIRA_TOKEN", raising=False)
        operations = PostPROperations(
            github_token="test-token",
            jira_url="https://test.atlassian.net",
            jira_token="",
            jira_email="test@example.com",
            slack_webhook="https://hooks.slack.com/test",
            memory_store_path=str(temp_dir / "memory.json"),
        )

        result = operations.jira_transition_issue(ticket_id="TICKET-456")

        assert result.status == OperationStatus.FAILED
        assert "JIRA token not configured" in result.message

    @patch("scripts.post_pr_operations.httpx.Client")
    def test_jira_transition_invalid_status(self, mock_client_class, operations):
        """Test JIRA transition fails when target status doesn't exist."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_get_response = Mock()
        mock_get_response.raise_for_status = Mock()
        mock_get_response.json.return_value = {
            "transitions": [
                {"id": "11", "to": {"name": "In Progress"}},
                {"id": "21", "to": {"name": "Code Review"}},
            ]
        }

        mock_client.get.return_value = mock_get_response

        result = operations.jira_transition_issue(ticket_id="TICKET-999", target_status="Nonexistent Status")

        assert result.status == OperationStatus.FAILED
        assert "Cannot transition to 'Nonexistent Status'" in result.message
        assert "Available transitions:" in result.message
        # Verify POST was never called since transition wasn't found
        mock_client.post.assert_not_called()

    @patch("scripts.post_pr_operations.httpx.Client")
    def test_jira_transition_custom_status(self, mock_client_class, operations):
        """Test JIRA transition to custom status."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_get_response = Mock()
        mock_get_response.raise_for_status = Mock()
        mock_get_response.json.return_value = {
            "transitions": [
                {"id": "11", "to": {"name": "In Progress"}},
                {"id": "21", "to": {"name": "Code Review"}},
            ]
        }

        mock_post_response = Mock()
        mock_post_response.raise_for_status = Mock()

        mock_client.get.return_value = mock_get_response
        mock_client.post.return_value = mock_post_response

        result = operations.jira_transition_issue(ticket_id="TICKET-789", target_status="In Progress")

        assert result.status == OperationStatus.SUCCESS
        assert result.details["status"] == "In Progress"

        # Verify correct transition ID was used for "In Progress"
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[1]["json"]["transition"]["id"] == "11"


class TestJiraAddComment:
    """Test jira_add_comment operation."""

    @patch("scripts.post_pr_operations.httpx.Client")
    def test_jira_add_comment_success(self, mock_client_class, operations):
        """Test successful JIRA comment."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_post_response = Mock()
        mock_post_response.raise_for_status = Mock()

        mock_client.post.return_value = mock_post_response

        result = operations.jira_add_comment(
            ticket_id="TICKET-123", pr_url="https://github.com/test/repo/pull/1", summary="Test PR summary"
        )

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "jira_add_comment"
        assert "TICKET-123" in result.message
        assert "Test PR summary" in result.details["comment"]
        assert "https://github.com/test/repo/pull/1" in result.details["comment"]

        # Verify POST request with ADF format comment (API v3 with Basic auth)
        expected_comment_adf = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "Pull Request created: https://github.com/test/repo/pull/1"}
                        ],
                    },
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Summary: Test PR summary"}],
                    },
                ],
            }
        }
        mock_client.post.assert_called_once_with(
            "https://test-jira.example.com/rest/api/3/issue/TICKET-123/comment",
            headers={
                "Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTp0ZXN0LWppcmEtdG9rZW4=",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=expected_comment_adf,
            timeout=30.0,
        )

    def test_jira_add_comment_no_token(self, temp_dir, monkeypatch):
        """Test JIRA comment fails without token."""
        monkeypatch.delenv("POST_PR_JIRA_TOKEN", raising=False)
        operations = PostPROperations(
            github_token="test-token",
            jira_url="https://test.atlassian.net",
            jira_token="",
            jira_email="test@example.com",
            slack_webhook="https://hooks.slack.com/test",
            memory_store_path=str(temp_dir / "memory.json"),
        )

        result = operations.jira_add_comment(
            ticket_id="TICKET-456", pr_url="https://github.com/test/repo/pull/2", summary="Test"
        )

        assert result.status == OperationStatus.FAILED
        assert "JIRA token not configured" in result.message


class TestSlackNotify:
    """Test slack_notify operation."""

    @patch("scripts.post_pr_operations.httpx.Client")
    def test_slack_notify_success(self, mock_client_class, operations):
        """Test successful Slack notification."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_post_response = Mock()
        mock_post_response.raise_for_status = Mock()

        mock_client.post.return_value = mock_post_response

        result = operations.slack_notify(
            pr_url="https://github.com/test/repo/pull/1", pr_number=1, summary="Test PR", channel="#test-channel"
        )

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "slack_notify"
        assert "#test-channel" in result.message
        assert result.details["channel"] == "#test-channel"

        # Verify Slack webhook POST call
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args

        # Verify webhook URL
        assert call_args[0][0] == "https://hooks.slack.com/test"

        # Verify timeout
        assert call_args[1]["timeout"] == 30.0

        # Verify message structure
        message_json = call_args[1]["json"]
        assert message_json["channel"] == "#test-channel"
        assert message_json["text"] == "New PR created: #1"
        assert "attachments" in message_json
        assert len(message_json["attachments"]) == 1

        # Verify attachment details
        attachment = message_json["attachments"][0]
        assert attachment["color"] == "good"

        # Verify fields in attachment
        fields = attachment["fields"]
        assert len(fields) == 2
        assert fields[0]["title"] == "PR"
        assert "<https://github.com/test/repo/pull/1|#1>" in fields[0]["value"]
        assert fields[0]["short"] is True
        assert fields[1]["title"] == "Summary"
        assert fields[1]["value"] == "Test PR"
        assert fields[1]["short"] is False

    def test_slack_notify_no_webhook(self, temp_dir, monkeypatch):
        """Test Slack notification fails without webhook."""
        monkeypatch.delenv("POST_PR_SLACK_WEBHOOK", raising=False)
        operations = PostPROperations(
            github_token="test-token",
            jira_url="https://test.atlassian.net",
            jira_token="test-jira-token",
            jira_email="test@example.com",
            slack_webhook="",
            memory_store_path=str(temp_dir / "memory.json"),
        )

        result = operations.slack_notify(
            pr_url="https://github.com/test/repo/pull/2", pr_number=2, summary="Test", channel="#test"
        )

        assert result.status == OperationStatus.FAILED
        assert "Slack webhook not configured" in result.message

    @patch("scripts.post_pr_operations.httpx.Client")
    def test_slack_notify_default_channel(self, mock_client_class, operations):
        """Test Slack notification with default channel."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_post_response = Mock()
        mock_post_response.raise_for_status = Mock()

        mock_client.post.return_value = mock_post_response

        result = operations.slack_notify(pr_url="https://github.com/test/repo/pull/3", pr_number=3, summary="Test PR")

        assert result.status == OperationStatus.SUCCESS
        assert result.details["channel"] == "#hcc-ai-assistant"

        # Verify default channel was used in the message
        call_args = mock_client.post.call_args
        message_json = call_args[1]["json"]
        assert message_json["channel"] == "#hcc-ai-assistant"


class TestMemoryStore:
    """Test memory_store operation."""

    def test_memory_store_success(self, operations):
        """Test successful memory storage."""
        learnings = {
            "patterns": ["Use async/await"],
            "gotchas": ["Watch for race conditions"],
            "decisions": ["Chose FastAPI"],
        }

        result = operations.memory_store(
            pr_url="https://github.com/test/repo/pull/1", ticket_id="TICKET-123", learnings=learnings
        )

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "memory_store"
        assert result.details["learnings"] == learnings

        # Verify file was written
        memory_file = Path(operations.memory_store_path)
        assert memory_file.exists()
        with open(memory_file, "r") as f:
            memories = json.load(f)
            assert len(memories) == 1
            assert memories[0]["ticket_id"] == "TICKET-123"
            assert memories[0]["learnings"] == learnings

    def test_memory_store_append(self, operations):
        """Test memory storage appends to existing file."""
        # First entry
        operations.memory_store(
            pr_url="https://github.com/test/repo/pull/1",
            ticket_id="TICKET-123",
            learnings={"patterns": ["Pattern 1"]},
        )

        # Second entry
        operations.memory_store(
            pr_url="https://github.com/test/repo/pull/2",
            ticket_id="TICKET-456",
            learnings={"patterns": ["Pattern 2"]},
        )

        # Verify both entries exist
        with open(operations.memory_store_path, "r") as f:
            memories = json.load(f)
            assert len(memories) == 2
            assert memories[0]["ticket_id"] == "TICKET-123"
            assert memories[1]["ticket_id"] == "TICKET-456"

    def test_memory_store_dry_run(self, temp_dir):
        """Test memory storage in dry-run mode."""
        operations = PostPROperations(
            github_token="test-token",
            jira_url="https://test.atlassian.net",
            jira_token="test-jira-token",
            jira_email="test@example.com",
            slack_webhook="https://hooks.slack.com/test",
            memory_store_path=str(temp_dir / "memory.json"),
            dry_run=True,
        )

        result = operations.memory_store(
            pr_url="https://github.com/test/repo/pull/3", ticket_id="TICKET-789", learnings={"patterns": []}
        )

        assert result.status == OperationStatus.SUCCESS
        # File should not be created in dry-run mode
        assert not Path(operations.memory_store_path).exists()


class TestBotStatusUpdate:
    """Test bot_status_update operation."""

    def test_bot_status_update_success(self, operations):
        """Test successful bot status update."""
        result = operations.bot_status_update(status="idle")

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "bot_status_update"
        assert "idle" in result.message
        assert result.details["status"] == "idle"

        # Verify status file was written
        status_file = Path("/tmp/bot_status.json")
        assert status_file.exists()
        with open(status_file, "r") as f:
            data = json.load(f)
            assert data["status"] == "idle"

    def test_bot_status_update_custom_status(self, operations):
        """Test bot status update with custom status."""
        result = operations.bot_status_update(status="working")

        assert result.status == OperationStatus.SUCCESS
        assert result.details["status"] == "working"

    def test_bot_status_update_dry_run(self, temp_dir):
        """Test bot status update in dry-run mode."""
        operations = PostPROperations(
            github_token="test-token",
            jira_url="https://test.atlassian.net",
            jira_token="test-jira-token",
            jira_email="test@example.com",
            slack_webhook="https://hooks.slack.com/test",
            memory_store_path=str(temp_dir / "memory.json"),
            dry_run=True,
        )

        result = operations.bot_status_update(status="idle")

        assert result.status == OperationStatus.SUCCESS


class TestOperationResult:
    """Test OperationResult dataclass."""

    def test_operation_result_creation(self):
        """Test creating an OperationResult."""
        result = OperationResult(
            operation="test_op", status=OperationStatus.SUCCESS, message="Test message", details={"key": "value"}
        )

        assert result.operation == "test_op"
        assert result.status == OperationStatus.SUCCESS
        assert result.message == "Test message"
        assert result.details == {"key": "value"}
        assert result.timestamp  # Should have a timestamp

    def test_operation_result_failed_status(self):
        """Test OperationResult with failed status."""
        result = OperationResult(operation="test_op", status=OperationStatus.FAILED, message="Error occurred")

        assert result.status == OperationStatus.FAILED
        assert "Error occurred" in result.message


class TestWorkflowResult:
    """Test WorkflowResult dataclass."""

    def test_workflow_result_to_dict(self):
        """Test converting WorkflowResult to dictionary."""
        operations = [
            OperationResult(operation="op1", status=OperationStatus.SUCCESS, message="OK"),
            OperationResult(operation="op2", status=OperationStatus.FAILED, message="Failed"),
        ]

        result = WorkflowResult(
            success=False,
            pr_url="https://github.com/test/repo/pull/1",
            pr_number=1,
            ticket_id="TICKET-123",
            operations=operations,
        )

        result_dict = result.to_dict()

        assert result_dict["success"] is False
        assert result_dict["pr_url"] == "https://github.com/test/repo/pull/1"
        assert result_dict["pr_number"] == 1
        assert result_dict["ticket_id"] == "TICKET-123"
        assert len(result_dict["operations"]) == 2
        assert result_dict["operations"][0]["status"] == "success"
        assert result_dict["operations"][1]["status"] == "failed"
