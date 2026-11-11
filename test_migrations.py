from unittest.mock import MagicMock, patch

import pytest

from migrations import (
    _find_kvstore_table,
    clear_stale_shield,
    get_db_config,
    run_migrations,
)

# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock()
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


@pytest.fixture
def mock_cursor(mock_conn):
    cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = cursor
    return cursor


# ============================================================================
# get_db_config TESTS
# ============================================================================


class TestGetDbConfig:

    def test_returns_none_when_no_pghost(self, monkeypatch):
        monkeypatch.delenv("PGHOST", raising=False)
        assert get_db_config() is None

    def test_returns_config_from_env_vars(self, monkeypatch):
        monkeypatch.setenv("PGHOST", "db.example.com")
        monkeypatch.setenv("PGPORT", "5433")
        monkeypatch.setenv("PGDATABASE", "mydb")
        monkeypatch.setenv("PGUSER", "admin")
        monkeypatch.setenv("PGPASSWORD", "secret")
        monkeypatch.setenv("PGSSLMODE", "require")

        config = get_db_config()
        assert config == {
            "host": "db.example.com",
            "port": 5433,
            "dbname": "mydb",
            "user": "admin",
            "password": "secret",
            "sslmode": "require",
        }

    def test_uses_defaults(self, monkeypatch):
        monkeypatch.setenv("PGHOST", "localhost")
        monkeypatch.delenv("PGPORT", raising=False)
        monkeypatch.delenv("PGDATABASE", raising=False)
        monkeypatch.delenv("PGUSER", raising=False)
        monkeypatch.delenv("PGPASSWORD", raising=False)
        monkeypatch.delenv("PGSSLMODE", raising=False)

        config = get_db_config()
        assert config["port"] == 5432
        assert config["dbname"] == "hcc-ai-assistant"
        assert config["user"] == "postgres"
        assert config["password"] == ""
        assert config["sslmode"] == "prefer"


# ============================================================================
# _find_kvstore_table TESTS
# ============================================================================


class TestFindKvstoreTable:

    def test_finds_table(self, mock_conn, mock_cursor):
        mock_cursor.fetchone.return_value = ("llamastack_kvstore",)
        assert _find_kvstore_table(mock_conn) == "llamastack_kvstore"

    def test_returns_none_when_no_match(self, mock_conn, mock_cursor):
        mock_cursor.fetchone.return_value = None
        assert _find_kvstore_table(mock_conn) is None


# ============================================================================
# clear_stale_shield TESTS
# ============================================================================


class TestClearStaleShield:

    def test_deletes_stale_shield(self, mock_conn, mock_cursor):
        mock_cursor.fetchone.return_value = ("llamastack_kvstore",)
        mock_cursor.rowcount = 1

        clear_stale_shield(mock_conn)

        calls = mock_cursor.execute.call_args_list
        delete_call = [c for c in calls if "DELETE" in str(c)]
        assert len(delete_call) == 1
        args = delete_call[0]
        assert "%shield:gemini-shield%" in str(args)
        assert "%publishers/google/models/%" in str(args)

    def test_skips_correctly_registered_shield(self, mock_conn, mock_cursor):
        mock_cursor.fetchone.return_value = ("llamastack_kvstore",)
        mock_cursor.rowcount = 0
        clear_stale_shield(mock_conn)

    def test_skips_when_no_kvstore_table(self, mock_conn, mock_cursor):
        mock_cursor.fetchone.return_value = None

        clear_stale_shield(mock_conn)

        calls = mock_cursor.execute.call_args_list
        delete_calls = [c for c in calls if "DELETE" in str(c)]
        assert len(delete_calls) == 0


# ============================================================================
# run_migrations TESTS
# ============================================================================


class TestRunMigrations:

    def test_skips_when_no_db_config(self, monkeypatch):
        monkeypatch.delenv("PGHOST", raising=False)
        run_migrations()

    @patch("migrations.psycopg2")
    def test_skips_when_connection_fails(self, mock_psycopg2, monkeypatch):
        monkeypatch.setenv("PGHOST", "localhost")
        mock_psycopg2.connect.side_effect = Exception("connection refused")
        run_migrations()

    @patch("migrations.psycopg2")
    def test_runs_migrations_and_closes_conn(self, mock_psycopg2, monkeypatch):
        monkeypatch.setenv("PGHOST", "localhost")

        mock_conn = MagicMock()
        mock_psycopg2.connect.return_value = mock_conn

        cursor = MagicMock()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cursor)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = ctx

        cursor.fetchone.return_value = None

        run_migrations()

        mock_conn.close.assert_called_once()
