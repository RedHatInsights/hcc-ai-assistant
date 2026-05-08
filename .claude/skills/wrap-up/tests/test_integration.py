"""Integration tests for wrap-up workflow."""

from unittest import mock

import pytest

from scripts.wrap_up_operations import OperationStatus, execute_wrap_up_workflow


def test_full_workflow_success():
    """Test complete wrap-up workflow with all operations succeeding."""
    with mock.patch("httpx.Client") as mock_client, mock.patch("subprocess.run") as mock_subprocess:
        # Mock all HTTP responses
        mock_client_instance = mock_client.return_value.__enter__.return_value

        # Mock get_task_info
        mock_task_response = mock.Mock()
        mock_task_response.status_code = 200
        mock_task_response.json.return_value = {
            "jira_key": "RHCLOUD-123",
            "status": "merged",
            "pr_url": "https://github.com/example/repo/pull/456",
            "pr_number": 456,
            "repository": "example/repo",
            "branch": "bot/RHCLOUD-123",
        }

        # Mock JIRA GET transitions
        mock_jira_get_response = mock.Mock()
        mock_jira_get_response.status_code = 200
        mock_jira_get_response.json.return_value = {
            "transitions": [
                {"id": "31", "name": "Release Pending", "to": {"name": "Release Pending"}},
            ]
        }

        # Mock all POST/DELETE responses
        mock_success_response = mock.Mock()
        mock_success_response.status_code = 200
        mock_success_response.raise_for_status.return_value = None

        # Set up response sequence
        mock_client_instance.get.side_effect = [mock_task_response, mock_jira_get_response]
        mock_client_instance.post.return_value = mock_success_response
        mock_client_instance.delete.return_value = mock_success_response

        # Mock git command
        mock_subprocess.return_value = mock.Mock(returncode=0, stdout="", stderr="")

        # Execute workflow
        result = execute_wrap_up_workflow(
            jira_key="RHCLOUD-123",
            jira_url="https://test-jira.example.com",
            jira_token="test-token",
            jira_email="test@example.com",
            memory_url="https://test-memory.example.com",
            github_token="test-github-token",
        )

        # Verify workflow succeeded
        assert result.success is True
        assert len(result.operations) == 8
        assert all(op.status == OperationStatus.SUCCESS for op in result.operations)

        # Verify operation names
        operation_names = [op.operation for op in result.operations]
        assert operation_names == [
            "get_task_info",
            "jira_transition_issue",
            "jira_add_comment",
            "archive_task",
            "slack_notify",
            "delete_remote_branch",
            "delete_local_branch",
            "bot_status_update",
        ]


def test_workflow_fails_on_first_error():
    """Test workflow stops on first failure (fail-fast)."""
    with mock.patch("httpx.Client") as mock_client:
        mock_client_instance = mock_client.return_value.__enter__.return_value

        # Mock get_task_info to fail
        mock_error_response = mock.Mock()
        mock_error_response.status_code = 404
        mock_error_response.raise_for_status.side_effect = Exception("Not Found")
        mock_client_instance.get.return_value = mock_error_response

        # Execute workflow
        result = execute_wrap_up_workflow(
            jira_key="RHCLOUD-123",
            jira_url="https://test-jira.example.com",
            jira_token="test-token",
            jira_email="test@example.com",
            memory_url="https://test-memory.example.com",
            github_token="test-github-token",
        )

        # Verify workflow failed
        assert result.success is False
        assert len(result.operations) == 1  # Only first operation ran
        assert result.operations[0].status == OperationStatus.FAILED
        assert result.operations[0].operation == "get_task_info"


def test_workflow_fails_on_jira_transition():
    """Test workflow stops when JIRA transition fails."""
    with mock.patch("httpx.Client") as mock_client:
        mock_client_instance = mock_client.return_value.__enter__.return_value

        # Mock successful get_task_info
        mock_task_response = mock.Mock()
        mock_task_response.status_code = 200
        mock_task_response.json.return_value = {
            "jira_key": "RHCLOUD-123",
            "status": "merged",
            "pr_url": "https://github.com/example/repo/pull/456",
            "repository": "example/repo",
            "branch": "bot/RHCLOUD-123",
        }

        # Mock JIRA transitions with no "Release Pending" status
        mock_jira_get_response = mock.Mock()
        mock_jira_get_response.status_code = 200
        mock_jira_get_response.json.return_value = {
            "transitions": [
                {"id": "41", "name": "In Progress", "to": {"name": "In Progress"}},
            ]
        }

        mock_client_instance.get.side_effect = [mock_task_response, mock_jira_get_response]

        # Execute workflow
        result = execute_wrap_up_workflow(
            jira_key="RHCLOUD-123",
            jira_url="https://test-jira.example.com",
            jira_token="test-token",
            jira_email="test@example.com",
            memory_url="https://test-memory.example.com",
            github_token="test-github-token",
        )

        # Verify workflow stopped at JIRA transition
        assert result.success is False
        assert len(result.operations) == 2  # get_task_info + jira_transition_issue
        assert result.operations[0].status == OperationStatus.SUCCESS
        assert result.operations[1].status == OperationStatus.FAILED
        assert result.operations[1].operation == "jira_transition_issue"


def test_workflow_with_skip_operations():
    """Test workflow with specific operations skipped."""
    with mock.patch("httpx.Client") as mock_client, mock.patch("subprocess.run") as mock_subprocess:
        mock_client_instance = mock_client.return_value.__enter__.return_value

        # Mock get_task_info
        mock_task_response = mock.Mock()
        mock_task_response.status_code = 200
        mock_task_response.json.return_value = {
            "jira_key": "RHCLOUD-123",
            "status": "merged",
            "pr_url": "https://github.com/example/repo/pull/456",
            "repository": "example/repo",
            "branch": "bot/RHCLOUD-123",
        }

        # Mock other responses
        mock_success_response = mock.Mock()
        mock_success_response.status_code = 200
        mock_success_response.raise_for_status.return_value = None

        mock_client_instance.get.return_value = mock_task_response
        mock_client_instance.post.return_value = mock_success_response
        mock_client_instance.delete.return_value = mock_success_response
        mock_subprocess.return_value = mock.Mock(returncode=0)

        # Execute workflow with skip operations
        result = execute_wrap_up_workflow(
            jira_key="RHCLOUD-123",
            jira_url="https://test-jira.example.com",
            jira_token="test-token",
            jira_email="test@example.com",
            memory_url="https://test-memory.example.com",
            github_token="test-github-token",
            skip_operations=["jira_transition_issue", "slack_notify"],
        )

        # Verify workflow succeeded
        assert result.success is True
        assert len(result.operations) == 8

        # Verify skipped operations
        jira_transition_op = next(op for op in result.operations if op.operation == "jira_transition_issue")
        slack_notify_op = next(op for op in result.operations if op.operation == "slack_notify")

        assert jira_transition_op.status == OperationStatus.SKIPPED
        assert slack_notify_op.status == OperationStatus.SKIPPED


def test_workflow_dry_run():
    """Test workflow in dry-run mode."""
    # No mocking needed for dry-run
    result = execute_wrap_up_workflow(
        jira_key="RHCLOUD-123",
        jira_url="https://test-jira.example.com",
        jira_token="test-token",
        jira_email="test@example.com",
        memory_url="https://test-memory.example.com",
        github_token="test-github-token",
        dry_run=True,
    )

    # Verify all operations succeeded in dry-run
    assert result.success is True
    assert len(result.operations) == 8
    assert all(op.status == OperationStatus.SUCCESS for op in result.operations)


def test_workflow_missing_jira_token():
    """Test workflow fails when JIRA token is not configured."""
    with mock.patch.dict("os.environ", {}, clear=True):
        result = execute_wrap_up_workflow(
            jira_key="RHCLOUD-123",
            jira_token=None,  # No token
            jira_email="test@example.com",
            memory_url="https://test-memory.example.com",
        )

        assert result.success is False
        assert len(result.operations) == 1
        assert result.operations[0].status == OperationStatus.FAILED
        assert "JIRA token not configured" in result.operations[0].message


def test_workflow_missing_jira_email():
    """Test workflow fails when JIRA email is not configured."""
    with mock.patch.dict("os.environ", {}, clear=True):
        result = execute_wrap_up_workflow(
            jira_key="RHCLOUD-123",
            jira_token="test-token",
            jira_email=None,  # No email
            memory_url="https://test-memory.example.com",
        )

        assert result.success is False
        assert len(result.operations) == 1
        assert result.operations[0].status == OperationStatus.FAILED
        assert "JIRA email not configured" in result.operations[0].message


def test_workflow_missing_memory_url():
    """Test workflow fails when memory server URL is not configured."""
    with mock.patch.dict("os.environ", {}, clear=True):
        result = execute_wrap_up_workflow(
            jira_key="RHCLOUD-123",
            jira_token="test-token",
            jira_email="test@example.com",
            memory_url=None,  # No memory URL
        )

        assert result.success is False
        assert len(result.operations) == 1
        assert result.operations[0].status == OperationStatus.FAILED
        assert "Memory server URL not configured" in result.operations[0].message


def test_workflow_env_vars():
    """Test workflow uses environment variables for configuration."""
    with mock.patch.dict(
        "os.environ",
        {
            "WRAP_UP_JIRA_URL": "https://env-jira.example.com",
            "WRAP_UP_JIRA_TOKEN": "env-token",
            "WRAP_UP_JIRA_EMAIL": "env@example.com",
            "BOT_MEMORY_URL": "https://env-memory.example.com",
            "GITHUB_TOKEN": "env-github-token",
        },
    ), mock.patch("httpx.Client") as mock_client, mock.patch("subprocess.run") as mock_subprocess:
        mock_client_instance = mock_client.return_value.__enter__.return_value

        # Mock responses
        mock_task_response = mock.Mock()
        mock_task_response.status_code = 200
        mock_task_response.json.return_value = {
            "jira_key": "RHCLOUD-123",
            "status": "merged",
            "pr_url": "https://github.com/example/repo/pull/456",
            "repository": "example/repo",
            "branch": "bot/RHCLOUD-123",
        }

        mock_jira_get_response = mock.Mock()
        mock_jira_get_response.status_code = 200
        mock_jira_get_response.json.return_value = {
            "transitions": [{"id": "31", "name": "Release Pending", "to": {"name": "Release Pending"}}]
        }

        mock_success_response = mock.Mock()
        mock_success_response.status_code = 200
        mock_success_response.raise_for_status.return_value = None

        mock_client_instance.get.side_effect = [mock_task_response, mock_jira_get_response]
        mock_client_instance.post.return_value = mock_success_response
        mock_client_instance.delete.return_value = mock_success_response
        mock_subprocess.return_value = mock.Mock(returncode=0)

        # Execute workflow without explicit parameters
        result = execute_wrap_up_workflow(jira_key="RHCLOUD-123")

        # Verify workflow succeeded using env vars
        assert result.success is True


def test_workflow_parameter_precedence():
    """Test that explicit parameters take precedence over environment variables."""
    with mock.patch.dict(
        "os.environ",
        {
            "WRAP_UP_JIRA_TOKEN": "env-token",
            "WRAP_UP_JIRA_EMAIL": "env@example.com",
            "BOT_MEMORY_URL": "https://env-memory.example.com",
        },
    ), mock.patch("httpx.Client") as mock_client, mock.patch("subprocess.run") as mock_subprocess:
        mock_client_instance = mock_client.return_value.__enter__.return_value

        # Mock responses
        mock_task_response = mock.Mock()
        mock_task_response.status_code = 200
        mock_task_response.json.return_value = {
            "jira_key": "RHCLOUD-123",
            "status": "merged",
            "pr_url": "https://github.com/example/repo/pull/456",
            "repository": "example/repo",
            "branch": "bot/RHCLOUD-123",
        }

        mock_jira_get_response = mock.Mock()
        mock_jira_get_response.status_code = 200
        mock_jira_get_response.json.return_value = {
            "transitions": [{"id": "31", "name": "Release Pending", "to": {"name": "Release Pending"}}]
        }

        mock_success_response = mock.Mock()
        mock_success_response.status_code = 200
        mock_success_response.raise_for_status.return_value = None

        mock_client_instance.get.side_effect = [mock_task_response, mock_jira_get_response]
        mock_client_instance.post.return_value = mock_success_response
        mock_client_instance.delete.return_value = mock_success_response
        mock_subprocess.return_value = mock.Mock(returncode=0)

        # Execute workflow with explicit parameters that should override env vars
        result = execute_wrap_up_workflow(
            jira_key="RHCLOUD-123",
            jira_url="https://param-jira.example.com",  # Override env var
            jira_token="param-token",  # Override env var
            jira_email="param@example.com",  # Override env var
            memory_url="https://param-memory.example.com",  # Override env var
            github_token="param-github-token",
        )

        # Verify workflow succeeded and used parameter values
        assert result.success is True

        # Verify URLs used in API calls match parameters, not env vars
        get_calls = mock_client_instance.get.call_args_list
        assert "https://param-memory.example.com" in str(get_calls[0])
        assert "https://param-jira.example.com" in str(get_calls[1])
