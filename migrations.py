"""Idempotent database migrations for hcc-ai-assistant.

All migrations run on every startup. Each must be safe to re-run.
Skipped entirely when no PG* env vars are configured (local dev with sqlite).
"""

import os

import psycopg2
from psycopg2 import sql

LOG_PREFIX = "[migrations]"


def get_db_config():
    """Read PG* env vars. Returns config dict or None if not configured."""
    host = os.environ.get("PGHOST")
    if not host:
        return None

    return {
        "host": host,
        "port": int(os.environ.get("PGPORT", "5432")),
        "dbname": os.environ.get("PGDATABASE", "hcc-ai-assistant"),
        "user": os.environ.get("PGUSER", "postgres"),
        "password": os.environ.get("PGPASSWORD", ""),
        "sslmode": os.environ.get("PGSSLMODE", "prefer"),
    }


def _find_kvstore_table(conn):
    """Find the llama-stack KV store table by matching its schema pattern.

    The KV store table has columns: key (text), value (text), expiration (timestamp).
    The table name varies by llama-stack version (e.g., kvstore, llamastack_kvstore, ogx_kvstore).
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT t.table_name
            FROM information_schema.tables t
            WHERE t.table_schema = 'public'
              AND t.table_type = 'BASE TABLE'
              AND EXISTS (
                  SELECT 1 FROM information_schema.columns c
                  WHERE c.table_name = t.table_name AND c.column_name = 'key' AND c.data_type = 'text'
              )
              AND EXISTS (
                  SELECT 1 FROM information_schema.columns c
                  WHERE c.table_name = t.table_name AND c.column_name = 'value' AND c.data_type = 'text'
              )
              AND EXISTS (
                  SELECT 1 FROM information_schema.columns c
                  WHERE c.table_name = t.table_name AND c.column_name = 'expiration'
              )
              AND t.table_name LIKE '%kvstore%'
        """)
        row = cur.fetchone()
        return row[0] if row else None


def clear_stale_shield(conn):
    """Delete stale gemini-shield registration from the llama-stack KV store.

    When upgrading from llama-stack 0.5.x to 0.6.x, the Vertex AI model ID
    format changed from 'google/gemini-...' to 'publishers/google/models/gemini-...'.
    The old shield registration conflicts with re-registration under the new format.

    Only deletes if the stored value still contains the old format. Once
    lightspeed-stack re-registers with the correct format, this is a no-op.
    """
    table = _find_kvstore_table(conn)
    if not table:
        return

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("DELETE FROM {} WHERE key LIKE %s AND value NOT LIKE %s").format(sql.Identifier(table)),
            ("%shield:gemini-shield%", "%publishers/google/models/%"),
        )
        if cur.rowcount > 0:
            print(f"{LOG_PREFIX} Deleted {cur.rowcount} stale gemini-shield registration(s) from {table}")


MIGRATIONS = [
    clear_stale_shield,
]


def run_migrations():
    """Connect to the database and run all migrations."""
    db_config = get_db_config()
    if not db_config:
        print(f"{LOG_PREFIX} No PostgreSQL config found, skipping migrations")
        return

    try:
        conn = psycopg2.connect(**db_config)
        conn.autocommit = True
    except Exception as e:  # noqa: BLE001 - migration failures must not block service startup
        print(f"{LOG_PREFIX} Could not connect to database: {e}")
        return

    try:
        for migration_fn in MIGRATIONS:
            migration_fn(conn)
    finally:
        conn.close()
