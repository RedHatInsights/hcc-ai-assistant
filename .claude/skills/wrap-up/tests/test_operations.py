"""Unit tests for wrap-up operations."""

import base64
from unittest import mock

import pytest

from scripts.wrap_up_operations import (
    OperationStatus,
    WrapUpOperations,
)


@pytest.fixture
def temp_dir(tmp_path):
    """Create a temporary directory for test files."""
    return tmp_path


@pytest.fixture
def operations(temp_dir):
    """Create WrapUpOperations instance for testing."""
    return WrapUpOperations(
        jira_url="https://test-jira.example.com",
        jira_token="test-jira-token",
        jira_email="test@example.com",
        memory_url="https://test-memory.example.com",
        github_token="test-github-token",
        project_repos_path=str(temp_dir / "project-repos.json"),
        dry_run=False,
    )


# Test get_task_info


def test_get_task_info_success(operations):
    """Test successful task info retrieval from memory server."""
    with mock.patch("httpx.Client") as mock_client:
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jira_key": "RHCLOUD-123",
            "status": "merged",
            "pr_url": "https://github.com/example/repo/pull/456",
            "pr_number": 456,
            "repository": "example/repo",
            "branch": "bot/RHCLOUD-123",
            "metadata": {},
        }
        mock_client.return_value.__enter__.return_value.get.return_value = mock_response

        result = operations.get_task_info("RHCLOUD-123")

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "get_task_info"
        assert "Retrieved task info" in result.message
        assert operations.task_info is not None
        assert operations.task_info["jira_key"] == "RHCLOUD-123"

        # Verify exact API call
        mock_client.return_value.__enter__.return_value.get.assert_called_once_with(
            "https://test-memory.example.com/tasks/RHCLOUD-123"
        )


def test_get_task_info_http_error(operations):
    """Test get_task_info handles HTTP errors."""
    with mock.patch("httpx.Client") as mock_client:
        mock_response = mock.Mock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = Exception("Not Found")
        mock_client.return_value.__enter__.return_value.get.return_value = mock_response

        result = operations.get_task_info("RHCLOUD-123")

        assert result.status == OperationStatus.FAILED
        assert "Failed to get task info" in result.message


# Test jira_transition_issue


def test_jira_transition_issue_success(operations):
    """Test successful JIRA issue transition to Release Pending."""
    with mock.patch("httpx.Client") as mock_client:
        # Mock GET transitions response
        mock_get_response = mock.Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {
            "transitions": [
                {"id": "31", "name": "Release Pending", "to": {"name": "Release Pending"}},
                {"id": "41", "name": "In Progress", "to": {"name": "In Progress"}},
            ]
        }

        # Mock POST transition response
        mock_post_response = mock.Mock()
        mock_post_response.status_code = 204
        mock_post_response.raise_for_status.return_value = None

        mock_client_instance = mock_client.return_value.__enter__.return_value
        mock_client_instance.get.return_value = mock_get_response
        mock_client_instance.post.return_value = mock_post_response

        result = operations.jira_transition_issue("RHCLOUD-123", "Release Pending")

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "jira_transition_issue"
        assert "Transitioned RHCLOUD-123" in result.message

        # Verify GET request to fetch available transitions (API v3 with Basic auth)
        expected_basic_auth = base64.b64encode(b"test@example.com:test-jira-token").decode()
        mock_client_instance.get.assert_called_once_with(
            "https://test-jira.example.com/rest/api/3/issue/RHCLOUD-123/transitions",
            headers={
                "Authorization": f"Basic {expected_basic_auth}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

        # Verify POST request to execute transition
        mock_client_instance.post.assert_called_once_with(
            "https://test-jira.example.com/rest/api/3/issue/RHCLOUD-123/transitions",
            headers={
                "Authorization": f"Basic {expected_basic_auth}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"transition": {"id": "31"}},
            timeout=30.0,
        )


def test_jira_transition_issue_not_found(operations):
    """Test jira_transition_issue when target status is not available."""
    with mock.patch("httpx.Client") as mock_client:
        mock_get_response = mock.Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {
            "transitions": [
                {"id": "41", "name": "In Progress", "to": {"name": "In Progress"}},
            ]
        }

        mock_client.return_value.__enter__.return_value.get.return_value = mock_get_response

        result = operations.jira_transition_issue("RHCLOUD-123", "Release Pending")

        assert result.status == OperationStatus.FAILED
        assert "not found" in result.message


# Test jira_add_comment


def test_jira_add_comment_success(operations):
    """Test successful JIRA comment addition with ADF format."""
    # Set task info first
    operations.task_info = {
        "pr_url": "https://github.com/example/repo/pull/123",
    }

    with mock.patch("httpx.Client") as mock_client:
        mock_response = mock.Mock()
        mock_response.status_code = 201
        mock_response.raise_for_status.return_value = None
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = operations.jira_add_comment("RHCLOUD-123")

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "jira_add_comment"
        assert "Added merge confirmation" in result.message

        # Verify exact API call with ADF format
        expected_basic_auth = base64.b64encode(b"test@example.com:test-jira-token").decode()
        expected_comment_adf = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "Pull Request merged: https://github.com/example/repo/pull/123"}
                        ],
                    },
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "Changes will be deployed to stage environment in the next release.",
                            }
                        ],
                    },
                ],
            }
        }

        mock_client.return_value.__enter__.return_value.post.assert_called_once_with(
            "https://test-jira.example.com/rest/api/3/issue/RHCLOUD-123/comment",
            headers={
                "Authorization": f"Basic {expected_basic_auth}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=expected_comment_adf,
            timeout=30.0,
        )


def test_jira_add_comment_no_task_info(operations):
    """Test jira_add_comment fails when task info is not available."""
    result = operations.jira_add_comment("RHCLOUD-123")

    assert result.status == OperationStatus.FAILED
    assert "Task info not available" in result.message


# Test archive_task


def test_archive_task_success(operations):
    """Test successful task archiving in memory server."""
    with mock.patch("httpx.Client") as mock_client:
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = operations.archive_task("RHCLOUD-123")

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "archive_task"
        assert "Archived task" in result.message

        # Verify exact API call
        mock_client.return_value.__enter__.return_value.post.assert_called_once_with(
            "https://test-memory.example.com/tasks/RHCLOUD-123/archive"
        )


def test_archive_task_http_error(operations):
    """Test archive_task handles HTTP errors."""
    with mock.patch("httpx.Client") as mock_client:
        mock_response = mock.Mock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = Exception("Server Error")
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = operations.archive_task("RHCLOUD-123")

        assert result.status == OperationStatus.FAILED
        assert "Failed to archive task" in result.message


# Test slack_notify


def test_slack_notify_success(operations):
    """Test successful Slack notification via memory server."""
    # Set task info first
    operations.task_info = {
        "pr_url": "https://github.com/example/repo/pull/123",
    }

    with mock.patch("httpx.Client") as mock_client:
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = operations.slack_notify("RHCLOUD-123")

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "slack_notify"
        assert "Sent Slack notification" in result.message

        # Verify exact API call
        expected_payload = {
            "jira_key": "RHCLOUD-123",
            "event": "release_pending",
            "pr_url": "https://github.com/example/repo/pull/123",
            "message": "PR merged for RHCLOUD-123, moving to Release Pending",
        }

        mock_client.return_value.__enter__.return_value.post.assert_called_once_with(
            "https://test-memory.example.com/notifications/slack",
            json=expected_payload,
        )


def test_slack_notify_no_task_info(operations):
    """Test slack_notify fails when task info is not available."""
    result = operations.slack_notify("RHCLOUD-123")

    assert result.status == OperationStatus.FAILED
    assert "Task info not available" in result.message


# Test delete_remote_branch


def test_delete_remote_branch_success(operations):
    """Test successful remote branch deletion via GitHub API."""
    # Set task info first
    operations.task_info = {
        "repository": "example/repo",
        "branch": "bot/RHCLOUD-123",
    }

    with mock.patch("httpx.Client") as mock_client:
        mock_response = mock.Mock()
        mock_response.status_code = 204
        mock_response.raise_for_status.return_value = None
        mock_client.return_value.__enter__.return_value.delete.return_value = mock_response

        result = operations.delete_remote_branch()

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "delete_remote_branch"
        assert "Deleted remote branch" in result.message

        # Verify exact API call
        mock_client.return_value.__enter__.return_value.delete.assert_called_once_with(
            "https://api.github.com/repos/example/repo/git/refs/heads/bot/RHCLOUD-123",
            headers={
                "Authorization": "Bearer test-github-token",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )


def test_delete_remote_branch_404_already_deleted(operations):
    """Test delete_remote_branch handles 404 gracefully (already deleted)."""
    operations.task_info = {
        "repository": "example/repo",
        "branch": "bot/RHCLOUD-123",
    }

    with mock.patch("httpx.Client") as mock_client:
        mock_response = mock.Mock()
        mock_response.status_code = 404
        from httpx import HTTPStatusError, Request, Response

        mock_response.raise_for_status.side_effect = HTTPStatusError(
            "Not Found", request=Request("DELETE", "https://example.com"), response=Response(404)
        )
        mock_client.return_value.__enter__.return_value.delete.return_value = mock_response

        result = operations.delete_remote_branch()

        assert result.status == OperationStatus.SUCCESS
        assert "not found" in result.message.lower()


def test_delete_remote_branch_no_task_info(operations):
    """Test delete_remote_branch fails when task info is not available."""
    result = operations.delete_remote_branch()

    assert result.status == OperationStatus.FAILED
    assert "Task info not available" in result.message


def test_delete_remote_branch_no_github_token():
    """Test delete_remote_branch fails when GitHub token is not configured."""
    # Clear environment variables to ensure no token fallback
    with mock.patch.dict("os.environ", {}, clear=True):
        ops = WrapUpOperations(
            jira_url="https://test-jira.example.com",
            jira_token="test-jira-token",
            jira_email="test@example.com",
            memory_url="https://test-memory.example.com",
            github_token=None,  # No token
            dry_run=False,
        )
        ops.task_info = {
            "repository": "example/repo",
            "branch": "bot/RHCLOUD-123",
        }

        result = ops.delete_remote_branch()

        assert result.status == OperationStatus.FAILED
        assert "GitHub token not configured" in result.message


# Test delete_local_branch


def test_delete_local_branch_success(operations):
    """Test successful local branch deletion via git command."""
    # Set task info first
    operations.task_info = {
        "branch": "bot/RHCLOUD-123",
    }

    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")

        result = operations.delete_local_branch()

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "delete_local_branch"
        assert "Deleted local branch" in result.message

        # Verify exact command
        mock_run.assert_called_once_with(
            ["git", "branch", "-D", "bot/RHCLOUD-123"],
            capture_output=True,
            text=True,
            check=False,
        )


def test_delete_local_branch_not_found(operations):
    """Test delete_local_branch handles branch not found gracefully."""
    operations.task_info = {
        "branch": "bot/RHCLOUD-123",
    }

    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=1, stdout="", stderr="error: branch 'bot/RHCLOUD-123' not found")

        result = operations.delete_local_branch()

        assert result.status == OperationStatus.SUCCESS
        assert "not found" in result.message.lower()


def test_delete_local_branch_no_task_info(operations):
    """Test delete_local_branch fails when task info is not available."""
    result = operations.delete_local_branch()

    assert result.status == OperationStatus.FAILED
    assert "Task info not available" in result.message


# Test bot_status_update


def test_bot_status_update_success(operations):
    """Test successful bot status update to idle."""
    with mock.patch("httpx.Client") as mock_client:
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = operations.bot_status_update("idle")

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "bot_status_update"
        assert "Updated bot status to 'idle'" in result.message

        # Verify exact API call
        mock_client.return_value.__enter__.return_value.post.assert_called_once_with(
            "https://test-memory.example.com/bot/status",
            json={"status": "idle"},
        )


def test_bot_status_update_http_error(operations):
    """Test bot_status_update handles HTTP errors."""
    with mock.patch("httpx.Client") as mock_client:
        mock_response = mock.Mock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = Exception("Server Error")
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = operations.bot_status_update("idle")

        assert result.status == OperationStatus.FAILED
        assert "Failed to update bot status" in result.message


# Test dry-run mode


def test_dry_run_mode(temp_dir):
    """Test all operations in dry-run mode."""
    ops = WrapUpOperations(
        jira_url="https://test-jira.example.com",
        jira_token="test-jira-token",
        jira_email="test@example.com",
        memory_url="https://test-memory.example.com",
        github_token="test-github-token",
        dry_run=True,
    )

    # All operations should succeed without making actual API calls
    result1 = ops.get_task_info("RHCLOUD-123")
    assert result1.status == OperationStatus.SUCCESS

    result2 = ops.jira_transition_issue("RHCLOUD-123")
    assert result2.status == OperationStatus.SUCCESS

    result3 = ops.jira_add_comment("RHCLOUD-123")
    assert result3.status == OperationStatus.SUCCESS

    result4 = ops.archive_task("RHCLOUD-123")
    assert result4.status == OperationStatus.SUCCESS

    result5 = ops.slack_notify("RHCLOUD-123")
    assert result5.status == OperationStatus.SUCCESS

    result6 = ops.delete_remote_branch()
    assert result6.status == OperationStatus.SUCCESS

    result7 = ops.delete_local_branch()
    assert result7.status == OperationStatus.SUCCESS

    result8 = ops.bot_status_update()
    assert result8.status == OperationStatus.SUCCESS
