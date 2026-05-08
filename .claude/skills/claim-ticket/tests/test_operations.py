"""Unit tests for claim-ticket operations."""

import base64
from unittest.mock import Mock, patch

import pytest

from scripts.claim_ticket_operations import (
    ClaimTicketOperations,
    OperationStatus,
)


@pytest.fixture
def operations():
    """Create ClaimTicketOperations instance for testing."""
    return ClaimTicketOperations(
        jira_url="https://test-jira.example.com",
        jira_token="test-jira-token",
        jira_email="test@example.com",
        memory_url="https://test-memory.example.com",
        platform_ui_board_id=9297,
        default_board_id=8070,
        dry_run=False,
    )


@pytest.fixture(autouse=True)
def clear_bot_account_cache():
    """Clear bot account ID cache before each test."""
    ClaimTicketOperations._bot_account_id_cache = None
    yield
    ClaimTicketOperations._bot_account_id_cache = None


class TestGetBotAccountId:
    """Test get_bot_account_id operation."""

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_get_bot_account_id_success(self, mock_client_class, operations):
        """Test successful bot account ID retrieval."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = [{"accountId": "bot-account-123", "emailAddress": "test@example.com"}]

        mock_client.get.return_value = mock_response

        result = operations.get_bot_account_id()

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "get_bot_account_id"
        assert "bot-account-123" in result.message
        assert result.details["account_id"] == "bot-account-123"
        assert operations.bot_account_id == "bot-account-123"

        # Verify API call with Basic auth
        mock_client.get.assert_called_once_with(
            "https://test-jira.example.com/rest/api/3/user/search",
            params={"query": "test@example.com"},
            headers={
                "Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTp0ZXN0LWppcmEtdG9rZW4=",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_get_bot_account_id_caching(self, mock_client_class, operations):
        """Test bot account ID is cached across instances."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = [{"accountId": "bot-account-456"}]

        mock_client.get.return_value = mock_response

        # First call - should hit the API
        result1 = operations.get_bot_account_id()
        assert result1.status == OperationStatus.SUCCESS
        assert mock_client.get.call_count == 1

        # Second call - should use cache
        result2 = operations.get_bot_account_id()
        assert result2.status == OperationStatus.SUCCESS
        assert "cache" in result2.message.lower()
        assert mock_client.get.call_count == 1  # No additional API call

        # New instance - should still use cache
        new_operations = ClaimTicketOperations(
            jira_url="https://test-jira.example.com",
            jira_token="test-jira-token",
            jira_email="test@example.com",
            memory_url="https://test-memory.example.com",
        )
        result3 = new_operations.get_bot_account_id()
        assert result3.status == OperationStatus.SUCCESS
        assert "cache" in result3.message.lower()
        assert mock_client.get.call_count == 1  # Still no additional API call

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_get_bot_account_id_no_user_found(self, mock_client_class, operations):
        """Test when no user is found for the email."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = []  # Empty list

        mock_client.get.return_value = mock_response

        result = operations.get_bot_account_id()

        assert result.status == OperationStatus.FAILED
        assert "No user found for email" in result.message
        assert operations.bot_account_id is None

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_get_bot_account_id_http_error(self, mock_client_class, operations):
        """Test handling of HTTP errors."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        from httpx import HTTPStatusError, Request, Response

        mock_response = Response(status_code=401, request=Request("GET", "https://test.com"))
        mock_client.get.side_effect = HTTPStatusError(
            "Unauthorized", request=mock_response.request, response=mock_response
        )

        result = operations.get_bot_account_id()

        assert result.status == OperationStatus.FAILED
        assert "HTTP 401" in result.message

    def test_get_bot_account_id_dry_run(self, operations):
        """Test get_bot_account_id in dry-run mode."""
        operations.dry_run = True

        result = operations.get_bot_account_id()

        assert result.status == OperationStatus.SUCCESS
        assert "dry run" in result.message.lower()
        assert operations.bot_account_id == "dry-run-account-id"


class TestGetTransitions:
    """Test get_transitions operation."""

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_get_transitions_success(self, mock_client_class, operations):
        """Test successful transition retrieval."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {
            "transitions": [
                {"id": "11", "name": "To Do", "to": {"name": "To Do"}},
                {"id": "21", "name": "In Progress", "to": {"name": "In Progress"}},
                {"id": "31", "name": "Done", "to": {"name": "Done"}},
            ]
        }

        mock_client.get.return_value = mock_response

        result = operations.get_transitions("RHCLOUD-12345")

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "get_transitions"
        assert "21" in result.message
        assert result.details["transition_id"] == "21"
        assert operations.transition_id == "21"

        # Verify API call
        mock_client.get.assert_called_once_with(
            "https://test-jira.example.com/rest/api/3/issue/RHCLOUD-12345/transitions",
            headers={
                "Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTp0ZXN0LWppcmEtdG9rZW4=",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_get_transitions_by_name(self, mock_client_class, operations):
        """Test finding transition by name field."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {
            "transitions": [
                {"id": "11", "name": "In Progress", "to": {"name": "Working"}},
            ]
        }

        mock_client.get.return_value = mock_response

        result = operations.get_transitions("RHCLOUD-67890")

        assert result.status == OperationStatus.SUCCESS
        assert result.details["transition_id"] == "11"

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_get_transitions_not_found(self, mock_client_class, operations):
        """Test when 'In Progress' transition is not found."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {
            "transitions": [
                {"id": "11", "name": "To Do", "to": {"name": "To Do"}},
                {"id": "31", "name": "Done", "to": {"name": "Done"}},
            ]
        }

        mock_client.get.return_value = mock_response

        result = operations.get_transitions("RHCLOUD-99999")

        assert result.status == OperationStatus.FAILED
        assert "'In Progress' transition not found" in result.message
        assert "Available:" in result.message
        assert operations.transition_id is None

    def test_get_transitions_dry_run(self, operations):
        """Test get_transitions in dry-run mode."""
        operations.dry_run = True

        result = operations.get_transitions("RHCLOUD-11111")

        assert result.status == OperationStatus.SUCCESS
        assert "dry run" in result.message.lower()
        assert operations.transition_id == "dry-run-transition-id"


class TestAssignTicket:
    """Test assign_ticket operation."""

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_assign_ticket_success(self, mock_client_class, operations):
        """Test successful ticket assignment."""
        operations.bot_account_id = "bot-account-123"

        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_response = Mock()
        mock_response.raise_for_status = Mock()

        mock_client.put.return_value = mock_response

        result = operations.assign_ticket("RHCLOUD-12345")

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "assign_ticket"
        assert "RHCLOUD-12345" in result.message

        # Verify API call
        mock_client.put.assert_called_once_with(
            "https://test-jira.example.com/rest/api/3/issue/RHCLOUD-12345/assignee",
            headers={
                "Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTp0ZXN0LWppcmEtdG9rZW4=",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"accountId": "bot-account-123"},
        )

    def test_assign_ticket_no_account_id(self, operations):
        """Test assign_ticket fails when bot account ID is not set."""
        result = operations.assign_ticket("RHCLOUD-12345")

        assert result.status == OperationStatus.FAILED
        assert "Bot account ID not available" in result.message

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_assign_ticket_http_error(self, mock_client_class, operations):
        """Test handling of HTTP errors during assignment."""
        operations.bot_account_id = "bot-account-123"

        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        from httpx import HTTPStatusError, Request, Response

        mock_response = Response(status_code=404, request=Request("PUT", "https://test.com"))
        mock_client.put.side_effect = HTTPStatusError(
            "Not Found", request=mock_response.request, response=mock_response
        )

        result = operations.assign_ticket("RHCLOUD-12345")

        assert result.status == OperationStatus.FAILED
        assert "HTTP 404" in result.message

    def test_assign_ticket_dry_run(self, operations):
        """Test assign_ticket in dry-run mode."""
        operations.bot_account_id = "bot-account-123"
        operations.dry_run = True

        result = operations.assign_ticket("RHCLOUD-12345")

        assert result.status == OperationStatus.SUCCESS
        assert "dry run" in result.message.lower()


class TestTransitionToInProgress:
    """Test transition_to_in_progress operation."""

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_transition_to_in_progress_success(self, mock_client_class, operations):
        """Test successful transition to 'In Progress'."""
        operations.transition_id = "21"

        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_response = Mock()
        mock_response.raise_for_status = Mock()

        mock_client.post.return_value = mock_response

        result = operations.transition_to_in_progress("RHCLOUD-12345")

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "transition_to_in_progress"
        assert "RHCLOUD-12345" in result.message

        # Verify API call
        mock_client.post.assert_called_once_with(
            "https://test-jira.example.com/rest/api/3/issue/RHCLOUD-12345/transitions",
            headers={
                "Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTp0ZXN0LWppcmEtdG9rZW4=",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"transition": {"id": "21"}},
        )

    def test_transition_to_in_progress_no_transition_id(self, operations):
        """Test transition fails when transition ID is not set."""
        result = operations.transition_to_in_progress("RHCLOUD-12345")

        assert result.status == OperationStatus.FAILED
        assert "Transition ID not available" in result.message

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_transition_to_in_progress_http_error(self, mock_client_class, operations):
        """Test handling of HTTP errors during transition."""
        operations.transition_id = "21"

        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        from httpx import HTTPStatusError, Request, Response

        mock_response = Response(status_code=400, request=Request("POST", "https://test.com"))
        mock_client.post.side_effect = HTTPStatusError(
            "Bad Request", request=mock_response.request, response=mock_response
        )

        result = operations.transition_to_in_progress("RHCLOUD-12345")

        assert result.status == OperationStatus.FAILED
        assert "HTTP 400" in result.message

    def test_transition_to_in_progress_dry_run(self, operations):
        """Test transition in dry-run mode."""
        operations.transition_id = "21"
        operations.dry_run = True

        result = operations.transition_to_in_progress("RHCLOUD-12345")

        assert result.status == OperationStatus.SUCCESS
        assert "dry run" in result.message.lower()


class TestResolveBoard:
    """Test resolve_board operation."""

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_resolve_board_platform_ui_label(self, mock_client_class, operations):
        """Test board resolution with platform-experience-ui label."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {"fields": {"labels": ["platform-experience-ui", "bug", "high-priority"]}}

        mock_client.get.return_value = mock_response

        result = operations.resolve_board("RHCLOUD-12345")

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "resolve_board"
        assert result.details["board_id"] == 9297  # platform_ui_board_id
        assert "platform-experience-ui" in result.details["labels"]
        assert operations.board_id == 9297

        # Verify API call
        mock_client.get.assert_called_once_with(
            "https://test-jira.example.com/rest/api/3/issue/RHCLOUD-12345",
            params={"fields": "labels"},
            headers={
                "Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTp0ZXN0LWppcmEtdG9rZW4=",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_resolve_board_default(self, mock_client_class, operations):
        """Test board resolution without platform-experience-ui label."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {"fields": {"labels": ["bug", "high-priority"]}}

        mock_client.get.return_value = mock_response

        result = operations.resolve_board("RHCLOUD-12345")

        assert result.status == OperationStatus.SUCCESS
        assert result.details["board_id"] == 8070  # default_board_id
        assert operations.board_id == 8070

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_resolve_board_no_labels(self, mock_client_class, operations):
        """Test board resolution when ticket has no labels."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {"fields": {"labels": []}}

        mock_client.get.return_value = mock_response

        result = operations.resolve_board("RHCLOUD-12345")

        assert result.status == OperationStatus.SUCCESS
        assert result.details["board_id"] == 8070  # default_board_id

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_resolve_board_http_error(self, mock_client_class, operations):
        """Test handling of HTTP errors during board resolution."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        from httpx import HTTPStatusError, Request, Response

        mock_response = Response(status_code=404, request=Request("GET", "https://test.com"))
        mock_client.get.side_effect = HTTPStatusError(
            "Not Found", request=mock_response.request, response=mock_response
        )

        result = operations.resolve_board("RHCLOUD-12345")

        assert result.status == OperationStatus.FAILED
        assert "HTTP 404" in result.message

    def test_resolve_board_dry_run(self, operations):
        """Test resolve_board in dry-run mode."""
        operations.dry_run = True

        result = operations.resolve_board("RHCLOUD-12345")

        assert result.status == OperationStatus.SUCCESS
        assert "dry run" in result.message.lower()
        assert operations.board_id == 8070  # default_board_id


class TestGetActiveSprint:
    """Test get_active_sprint operation."""

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_get_active_sprint_success(self, mock_client_class, operations):
        """Test successful active sprint retrieval."""
        operations.board_id = 8070

        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {"values": [{"id": 12345, "name": "Sprint 42", "state": "active"}]}

        mock_client.get.return_value = mock_response

        result = operations.get_active_sprint()

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "get_active_sprint"
        assert result.details["sprint_id"] == 12345
        assert result.details["sprint_name"] == "Sprint 42"
        assert operations.sprint_id == 12345

        # Verify API call (Agile API v1.0)
        mock_client.get.assert_called_once_with(
            "https://test-jira.example.com/rest/agile/1.0/board/8070/sprint",
            params={"state": "active"},
            headers={
                "Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTp0ZXN0LWppcmEtdG9rZW4=",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_get_active_sprint_no_active(self, mock_client_class, operations):
        """Test when no active sprint exists."""
        operations.board_id = 8070

        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {"values": []}

        mock_client.get.return_value = mock_response

        result = operations.get_active_sprint()

        assert result.status == OperationStatus.FAILED
        assert "No active sprint found" in result.message
        assert operations.sprint_id is None

    def test_get_active_sprint_no_board_id(self, operations):
        """Test get_active_sprint fails when board ID is not set."""
        result = operations.get_active_sprint()

        assert result.status == OperationStatus.FAILED
        assert "Board ID not available" in result.message

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_get_active_sprint_http_error(self, mock_client_class, operations):
        """Test handling of HTTP errors during sprint retrieval."""
        operations.board_id = 8070

        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        from httpx import HTTPStatusError, Request, Response

        mock_response = Response(status_code=403, request=Request("GET", "https://test.com"))
        mock_client.get.side_effect = HTTPStatusError(
            "Forbidden", request=mock_response.request, response=mock_response
        )

        result = operations.get_active_sprint()

        assert result.status == OperationStatus.FAILED
        assert "HTTP 403" in result.message

    def test_get_active_sprint_dry_run(self, operations):
        """Test get_active_sprint in dry-run mode."""
        operations.board_id = 8070
        operations.dry_run = True

        result = operations.get_active_sprint()

        assert result.status == OperationStatus.SUCCESS
        assert "dry run" in result.message.lower()
        assert operations.sprint_id == 12345


class TestAddToSprint:
    """Test add_to_sprint operation."""

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_add_to_sprint_success(self, mock_client_class, operations):
        """Test successful adding ticket to sprint."""
        operations.sprint_id = 12345

        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_response = Mock()
        mock_response.raise_for_status = Mock()

        mock_client.post.return_value = mock_response

        result = operations.add_to_sprint("RHCLOUD-12345")

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "add_to_sprint"
        assert "RHCLOUD-12345" in result.message
        assert "12345" in result.message

        # Verify API call (Agile API v1.0)
        mock_client.post.assert_called_once_with(
            "https://test-jira.example.com/rest/agile/1.0/sprint/12345/issue",
            headers={
                "Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTp0ZXN0LWppcmEtdG9rZW4=",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"issues": ["RHCLOUD-12345"]},
        )

    def test_add_to_sprint_no_sprint_id(self, operations):
        """Test add_to_sprint fails when sprint ID is not set."""
        result = operations.add_to_sprint("RHCLOUD-12345")

        assert result.status == OperationStatus.FAILED
        assert "Sprint ID not available" in result.message

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_add_to_sprint_http_error(self, mock_client_class, operations):
        """Test handling of HTTP errors during sprint addition."""
        operations.sprint_id = 12345

        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        from httpx import HTTPStatusError, Request, Response

        mock_response = Response(status_code=400, request=Request("POST", "https://test.com"))
        mock_client.post.side_effect = HTTPStatusError(
            "Bad Request", request=mock_response.request, response=mock_response
        )

        result = operations.add_to_sprint("RHCLOUD-12345")

        assert result.status == OperationStatus.FAILED
        assert "HTTP 400" in result.message

    def test_add_to_sprint_dry_run(self, operations):
        """Test add_to_sprint in dry-run mode."""
        operations.sprint_id = 12345
        operations.dry_run = True

        result = operations.add_to_sprint("RHCLOUD-12345")

        assert result.status == OperationStatus.SUCCESS
        assert "dry run" in result.message.lower()


class TestTaskAdd:
    """Test task_add operation."""

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_task_add_success(self, mock_client_class, operations):
        """Test successful task addition to memory server."""
        operations.bot_account_id = "bot-account-123"
        operations.board_id = 8070
        operations.sprint_id = 12345

        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_response = Mock()
        mock_response.raise_for_status = Mock()

        mock_client.post.return_value = mock_response

        result = operations.task_add("RHCLOUD-12345")

        assert result.status == OperationStatus.SUCCESS
        assert result.operation == "task_add"
        assert "RHCLOUD-12345" in result.message

        # Verify API call
        mock_client.post.assert_called_once_with(
            "https://test-memory.example.com/tasks",
            json={
                "jira_key": "RHCLOUD-12345",
                "status": "in_progress",
                "assigned_to": "bot-account-123",
                "board_id": 8070,
                "sprint_id": 12345,
            },
        )

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_task_add_minimal_state(self, mock_client_class, operations):
        """Test task addition with minimal state (no IDs set)."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        mock_response = Mock()
        mock_response.raise_for_status = Mock()

        mock_client.post.return_value = mock_response

        result = operations.task_add("RHCLOUD-67890")

        assert result.status == OperationStatus.SUCCESS

        # Verify API call with fallback values
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        assert payload["jira_key"] == "RHCLOUD-67890"
        assert payload["status"] == "in_progress"
        assert payload["assigned_to"] == "unknown"
        assert payload["board_id"] is None
        assert payload["sprint_id"] is None

    @patch("scripts.claim_ticket_operations.httpx.Client")
    def test_task_add_http_error(self, mock_client_class, operations):
        """Test handling of HTTP errors during task addition."""
        # Mock HTTP responses
        mock_client = Mock()
        mock_client_class.return_value.__enter__.return_value = mock_client

        from httpx import HTTPStatusError, Request, Response

        mock_response = Response(status_code=500, request=Request("POST", "https://test.com"))
        mock_client.post.side_effect = HTTPStatusError(
            "Internal Server Error", request=mock_response.request, response=mock_response
        )

        result = operations.task_add("RHCLOUD-12345")

        assert result.status == OperationStatus.FAILED
        assert "HTTP 500" in result.message

    def test_task_add_dry_run(self, operations):
        """Test task_add in dry-run mode."""
        operations.bot_account_id = "bot-account-123"
        operations.board_id = 8070
        operations.sprint_id = 12345
        operations.dry_run = True

        result = operations.task_add("RHCLOUD-12345")

        assert result.status == OperationStatus.SUCCESS
        assert "dry run" in result.message.lower()


class TestAuthHeader:
    """Test authentication header generation."""

    def test_get_auth_header(self, operations):
        """Test Basic auth header is correctly encoded."""
        auth_header = operations._get_auth_header()

        # Verify header structure
        assert "Authorization" in auth_header
        assert "Accept" in auth_header
        assert "Content-Type" in auth_header

        # Verify Basic auth encoding
        expected_credentials = "test@example.com:test-jira-token"
        expected_encoded = base64.b64encode(expected_credentials.encode()).decode()
        assert auth_header["Authorization"] == f"Basic {expected_encoded}"
        assert auth_header["Accept"] == "application/json"
        assert auth_header["Content-Type"] == "application/json"
