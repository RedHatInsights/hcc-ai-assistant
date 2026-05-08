"""Integration tests for claim-ticket workflow."""

from unittest.mock import Mock, patch

import pytest

from scripts.claim_ticket_operations import (
    OperationStatus,
    execute_claim_ticket_workflow,
)


@pytest.fixture
def env_vars(monkeypatch):
    """Set up environment variables for testing."""
    monkeypatch.setenv("CLAIM_TICKET_JIRA_URL", "https://test-jira.example.com")
    monkeypatch.setenv("CLAIM_TICKET_JIRA_TOKEN", "test-jira-token")
    monkeypatch.setenv("CLAIM_TICKET_JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("BOT_MEMORY_URL", "https://test-memory.example.com")


@pytest.fixture
def mock_api():
    """Mock all API responses for a successful workflow."""
    with patch("scripts.claim_ticket_operations.httpx.Client") as mock_client_class:
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        # Mock all HTTP responses
        def get_side_effect(url, **kwargs):
            response = Mock()
            response.raise_for_status = Mock()

            if "/user/search" in url:
                # Bot account ID lookup
                response.json.return_value = [{"accountId": "bot-account-123"}]
            elif "/transitions" in url:
                # Get transitions
                response.json.return_value = {
                    "transitions": [{"id": "21", "name": "In Progress", "to": {"name": "In Progress"}}]
                }
            elif "fields=labels" in str(kwargs.get("params", {})):
                # Resolve board (check labels)
                response.json.return_value = {"fields": {"labels": ["bug"]}}
            elif "/board/" in url and "/sprint" in url:
                # Get active sprint
                response.json.return_value = {"values": [{"id": 12345, "name": "Sprint 42"}]}
            else:
                response.json.return_value = {}

            return response

        def post_side_effect(url, **kwargs):
            response = Mock()
            response.raise_for_status = Mock()
            return response

        def put_side_effect(url, **kwargs):
            response = Mock()
            response.raise_for_status = Mock()
            return response

        mock_client.get.side_effect = get_side_effect
        mock_client.post.side_effect = post_side_effect
        mock_client.put.side_effect = put_side_effect

        yield mock_client


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear bot account ID cache before each test."""
    from scripts.claim_ticket_operations import ClaimTicketOperations

    ClaimTicketOperations._bot_account_id_cache = None
    yield
    ClaimTicketOperations._bot_account_id_cache = None


class TestFullWorkflow:
    """Test complete claim-ticket workflow."""

    def test_full_workflow_success(self, env_vars, mock_api):
        """Test successful execution of all operations."""
        result = execute_claim_ticket_workflow(jira_key="RHCLOUD-12345")

        assert result.success is True
        assert result.jira_key == "RHCLOUD-12345"
        assert len(result.operations) == 8

        # Verify all operations succeeded
        for op in result.operations:
            assert op.status == OperationStatus.SUCCESS

        # Verify operation order
        expected_operations = [
            "get_bot_account_id",
            "get_transitions",
            "assign_ticket",
            "transition_to_in_progress",
            "resolve_board",
            "get_active_sprint",
            "add_to_sprint",
            "task_add",
        ]
        actual_operations = [op.operation for op in result.operations]
        assert actual_operations == expected_operations

        # Verify API calls were made
        assert mock_api.get.call_count >= 4  # bot account, transitions, labels, sprint
        assert mock_api.put.call_count == 1  # assign ticket
        assert mock_api.post.call_count == 3  # transition, add to sprint, task add

    def test_workflow_with_platform_ui_label(self, env_vars):
        """Test workflow with platform-experience-ui label."""
        with patch("scripts.claim_ticket_operations.httpx.Client") as mock_client_class:
            mock_client = Mock()
            mock_client_class.return_value.__enter__.return_value = mock_client

            # Create mock to return platform-experience-ui label
            def get_side_effect(url, params=None, **kwargs):
                response = Mock()
                response.raise_for_status = Mock()

                if "/user/search" in url:
                    response.json.return_value = [{"accountId": "bot-account-456"}]
                elif "/transitions" in url:
                    response.json.return_value = {
                        "transitions": [{"id": "21", "name": "In Progress", "to": {"name": "In Progress"}}]
                    }
                elif params and "labels" in str(params.get("fields", "")):
                    # Return platform-experience-ui label
                    response.json.return_value = {"fields": {"labels": ["platform-experience-ui", "enhancement"]}}
                elif "/board/" in url and "/sprint" in url:
                    response.json.return_value = {"values": [{"id": 99999, "name": "Platform UI Sprint"}]}
                else:
                    response.json.return_value = {}

                return response

            def post_side_effect(url, **kwargs):
                response = Mock()
                response.raise_for_status = Mock()
                return response

            def put_side_effect(url, **kwargs):
                response = Mock()
                response.raise_for_status = Mock()
                return response

            mock_client.get.side_effect = get_side_effect
            mock_client.post.side_effect = post_side_effect
            mock_client.put.side_effect = put_side_effect

            result = execute_claim_ticket_workflow(jira_key="RHCLOUD-54321")

            assert result.success is True

            # Verify board resolution used platform UI board
            resolve_board_op = next(op for op in result.operations if op.operation == "resolve_board")
            assert resolve_board_op.details["board_id"] == 9297  # platform_ui_board_id
            assert "platform-experience-ui" in resolve_board_op.details["labels"]

    def test_workflow_fails_on_first_error(self, env_vars, mock_api):
        """Test that workflow stops on first error (fail-fast)."""

        # Mock bot account lookup to fail
        def get_side_effect(url, **kwargs):
            response = Mock()
            response.raise_for_status = Mock()

            if "/user/search" in url:
                # Return empty list (no user found)
                response.json.return_value = []
            else:
                response.json.return_value = {}

            return response

        mock_api.get.side_effect = get_side_effect

        result = execute_claim_ticket_workflow(jira_key="RHCLOUD-99999")

        assert result.success is False

        # Find the failed operation
        failed_ops = [op for op in result.operations if op.status == OperationStatus.FAILED]
        assert len(failed_ops) == 1
        assert failed_ops[0].operation == "get_bot_account_id"

        # Verify workflow stopped after failure
        assert len(result.operations) == 1

    def test_workflow_with_skip_operations(self, env_vars, mock_api):
        """Test workflow with specific operations skipped."""
        result = execute_claim_ticket_workflow(jira_key="RHCLOUD-11111", skip_operations=["add_to_sprint", "task_add"])

        assert result.success is True

        # Verify skipped operations
        add_to_sprint_op = next(op for op in result.operations if op.operation == "add_to_sprint")
        assert add_to_sprint_op.status == OperationStatus.SKIPPED

        task_add_op = next(op for op in result.operations if op.operation == "task_add")
        assert task_add_op.status == OperationStatus.SKIPPED

        # Verify non-skipped operations succeeded
        for op in result.operations:
            if op.operation not in ["add_to_sprint", "task_add"]:
                assert op.status == OperationStatus.SUCCESS

    def test_workflow_dry_run(self, env_vars, mock_api):
        """Test workflow in dry-run mode."""
        result = execute_claim_ticket_workflow(jira_key="RHCLOUD-22222", dry_run=True)

        assert result.success is True

        # Verify all operations succeeded (dry-run should not fail)
        for op in result.operations:
            assert op.status == OperationStatus.SUCCESS

        # Verify no actual API calls were made (dry-run mode)
        assert mock_api.get.call_count == 0
        assert mock_api.put.call_count == 0
        assert mock_api.post.call_count == 0


class TestConfigValidation:
    """Test configuration validation in workflow."""

    def test_workflow_missing_jira_token(self, monkeypatch):
        """Test workflow fails when JIRA token is missing."""
        monkeypatch.delenv("CLAIM_TICKET_JIRA_TOKEN", raising=False)
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        monkeypatch.setenv("CLAIM_TICKET_JIRA_EMAIL", "test@example.com")
        monkeypatch.setenv("BOT_MEMORY_URL", "https://test-memory.example.com")

        result = execute_claim_ticket_workflow(jira_key="RHCLOUD-12345")

        assert result.success is False
        assert len(result.operations) == 1
        assert result.operations[0].operation == "config_validation"
        assert "JIRA token not configured" in result.operations[0].message

    def test_workflow_missing_jira_email(self, monkeypatch):
        """Test workflow fails when JIRA email is missing."""
        monkeypatch.setenv("CLAIM_TICKET_JIRA_TOKEN", "test-token")
        monkeypatch.delenv("CLAIM_TICKET_JIRA_EMAIL", raising=False)
        monkeypatch.setenv("BOT_MEMORY_URL", "https://test-memory.example.com")

        result = execute_claim_ticket_workflow(jira_key="RHCLOUD-12345")

        assert result.success is False
        assert len(result.operations) == 1
        assert result.operations[0].operation == "config_validation"
        assert "JIRA email not configured" in result.operations[0].message

    def test_workflow_missing_memory_url(self, monkeypatch):
        """Test workflow fails when memory URL is missing."""
        monkeypatch.setenv("CLAIM_TICKET_JIRA_TOKEN", "test-token")
        monkeypatch.setenv("CLAIM_TICKET_JIRA_EMAIL", "test@example.com")
        monkeypatch.delenv("BOT_MEMORY_URL", raising=False)

        result = execute_claim_ticket_workflow(jira_key="RHCLOUD-12345")

        assert result.success is False
        assert len(result.operations) == 1
        assert result.operations[0].operation == "config_validation"
        assert "Memory server URL not configured" in result.operations[0].message

    def test_workflow_fallback_to_jira_api_token(self, env_vars, mock_api, monkeypatch):
        """Test workflow can use JIRA_API_TOKEN as fallback."""
        monkeypatch.delenv("CLAIM_TICKET_JIRA_TOKEN", raising=False)
        monkeypatch.setenv("JIRA_API_TOKEN", "fallback-token")
        monkeypatch.setenv("CLAIM_TICKET_JIRA_EMAIL", "test@example.com")
        monkeypatch.setenv("BOT_MEMORY_URL", "https://test-memory.example.com")

        result = execute_claim_ticket_workflow(jira_key="RHCLOUD-12345")

        assert result.success is True


class TestParameterPrecedence:
    """Test parameter precedence (arguments vs environment variables)."""

    def test_explicit_params_override_env(self, env_vars, mock_api):
        """Test that explicit parameters override environment variables."""
        result = execute_claim_ticket_workflow(
            jira_key="RHCLOUD-33333",
            jira_url="https://override-jira.example.com",
            jira_token="override-token",
            jira_email="override@example.com",
            memory_url="https://override-memory.example.com",
        )

        assert result.success is True

        # Verify override URLs were used (check if operations would work)
        for op in result.operations:
            assert op.status == OperationStatus.SUCCESS

    def test_env_vars_used_when_no_params(self, env_vars, mock_api):
        """Test that environment variables are used when no params provided."""
        result = execute_claim_ticket_workflow(jira_key="RHCLOUD-44444")

        assert result.success is True

        # Verify env vars were used (workflow should succeed)
        for op in result.operations:
            assert op.status == OperationStatus.SUCCESS


class TestWorkflowEdgeCases:
    """Test edge cases and error scenarios."""

    def test_workflow_with_http_errors(self, env_vars, mock_api):
        """Test workflow handling of HTTP errors."""
        from httpx import HTTPStatusError, Request, Response

        # Mock transitions endpoint to fail
        def get_side_effect(url, **kwargs):
            response = Mock()
            response.raise_for_status = Mock()

            if "/user/search" in url:
                response.json.return_value = [{"accountId": "bot-account-123"}]
            elif "/transitions" in url:
                # Simulate HTTP error
                mock_response = Response(status_code=403, request=Request("GET", url))
                raise HTTPStatusError("Forbidden", request=mock_response.request, response=mock_response)
            else:
                response.json.return_value = {}

            return response

        mock_api.get.side_effect = get_side_effect

        result = execute_claim_ticket_workflow(jira_key="RHCLOUD-55555")

        assert result.success is False

        # Find the failed operation
        failed_ops = [op for op in result.operations if op.status == OperationStatus.FAILED]
        assert len(failed_ops) == 1
        assert failed_ops[0].operation == "get_transitions"
        assert "HTTP 403" in failed_ops[0].message

    def test_workflow_with_missing_transition(self, env_vars, mock_api):
        """Test workflow when 'In Progress' transition is not available."""

        def get_side_effect(url, **kwargs):
            response = Mock()
            response.raise_for_status = Mock()

            if "/user/search" in url:
                response.json.return_value = [{"accountId": "bot-account-123"}]
            elif "/transitions" in url:
                # Return transitions without "In Progress"
                response.json.return_value = {
                    "transitions": [
                        {"id": "11", "name": "To Do", "to": {"name": "To Do"}},
                        {"id": "31", "name": "Done", "to": {"name": "Done"}},
                    ]
                }
            else:
                response.json.return_value = {}

            return response

        mock_api.get.side_effect = get_side_effect

        result = execute_claim_ticket_workflow(jira_key="RHCLOUD-66666")

        assert result.success is False

        # Find the failed operation
        failed_ops = [op for op in result.operations if op.status == OperationStatus.FAILED]
        assert len(failed_ops) == 1
        assert failed_ops[0].operation == "get_transitions"
        assert "'In Progress' transition not found" in failed_ops[0].message

    def test_workflow_with_no_active_sprint(self, env_vars, mock_api):
        """Test workflow when no active sprint exists."""

        def get_side_effect(url, **kwargs):
            response = Mock()
            response.raise_for_status = Mock()

            if "/user/search" in url:
                response.json.return_value = [{"accountId": "bot-account-123"}]
            elif "/transitions" in url:
                response.json.return_value = {
                    "transitions": [{"id": "21", "name": "In Progress", "to": {"name": "In Progress"}}]
                }
            elif "fields=labels" in str(kwargs.get("params", {})):
                response.json.return_value = {"fields": {"labels": []}}
            elif "/board/" in url and "/sprint" in url:
                # Return empty sprints (no active sprint)
                response.json.return_value = {"values": []}
            else:
                response.json.return_value = {}

            return response

        mock_api.get.side_effect = get_side_effect

        result = execute_claim_ticket_workflow(jira_key="RHCLOUD-77777")

        assert result.success is False

        # Find the failed operation
        failed_ops = [op for op in result.operations if op.status == OperationStatus.FAILED]
        assert len(failed_ops) == 1
        assert failed_ops[0].operation == "get_active_sprint"
        assert "No active sprint found" in failed_ops[0].message

    def test_workflow_with_special_characters_in_jira_key(self, env_vars, mock_api):
        """Test workflow with special characters in JIRA key."""
        result = execute_claim_ticket_workflow(jira_key="RHCLOUD-12345-SPECIAL")

        assert result.success is True
        assert result.jira_key == "RHCLOUD-12345-SPECIAL"


class TestCaching:
    """Test bot account ID caching across workflow executions."""

    def test_bot_account_cached_across_executions(self, env_vars, mock_api):
        """Test that bot account ID is cached across multiple workflow executions."""
        # First execution
        result1 = execute_claim_ticket_workflow(jira_key="RHCLOUD-10001")
        assert result1.success is True

        # Count initial API calls
        initial_get_count = mock_api.get.call_count

        # Second execution
        result2 = execute_claim_ticket_workflow(jira_key="RHCLOUD-10002")
        assert result2.success is True

        # Verify bot account lookup was not called again
        # First execution: user search, transitions, labels, sprint (4 calls)
        # Second execution: should skip user search (only 3 new calls)
        expected_additional_calls = 3  # transitions, labels, sprint
        actual_additional_calls = mock_api.get.call_count - initial_get_count
        assert actual_additional_calls == expected_additional_calls

        # Verify both results have bot account set
        bot_account_op1 = next(op for op in result1.operations if op.operation == "get_bot_account_id")
        bot_account_op2 = next(op for op in result2.operations if op.operation == "get_bot_account_id")

        assert "bot-account-123" in bot_account_op1.message
        assert "cache" in bot_account_op2.message.lower()


class TestWorkflowResult:
    """Test WorkflowResult structure and serialization."""

    def test_workflow_result_structure(self, env_vars, mock_api):
        """Test that WorkflowResult has correct structure."""
        result = execute_claim_ticket_workflow(jira_key="RHCLOUD-88888")

        assert hasattr(result, "success")
        assert hasattr(result, "operations")
        assert hasattr(result, "jira_key")
        assert isinstance(result.success, bool)
        assert isinstance(result.operations, list)
        assert isinstance(result.jira_key, str)

    def test_operation_result_details(self, env_vars, mock_api):
        """Test that OperationResult includes detailed information."""
        result = execute_claim_ticket_workflow(jira_key="RHCLOUD-99999")

        # Check get_bot_account_id details
        bot_account_op = next(op for op in result.operations if op.operation == "get_bot_account_id")
        assert "account_id" in bot_account_op.details

        # Check get_transitions details
        transitions_op = next(op for op in result.operations if op.operation == "get_transitions")
        assert "transition_id" in transitions_op.details

        # Check resolve_board details
        resolve_board_op = next(op for op in result.operations if op.operation == "resolve_board")
        assert "board_id" in resolve_board_op.details
        assert "labels" in resolve_board_op.details

        # Check get_active_sprint details
        sprint_op = next(op for op in result.operations if op.operation == "get_active_sprint")
        assert "sprint_id" in sprint_op.details
