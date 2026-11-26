import os
import sqlite3
from contextlib import closing


class Conversations:
    """Store serialized agent history by conversation ID, owner key, and revision."""

    def __init__(self):
        """Create the conversation table if needed, without modifying existing histories.

        Use PostgreSQL when PGHOST is set. Otherwise, require a persistent
        AGENT_SQLITE_PATH; a missing path or an in-memory database raises ValueError.
        """
        if os.environ.get("PGHOST"):
            import psycopg2

            self.connect = psycopg2.connect
            self.placeholder = "%s"
        else:
            path = os.environ.get("AGENT_SQLITE_PATH")
            if not path or path == ":memory:":
                raise ValueError(
                    "Set PGHOST or an explicit persistent AGENT_SQLITE_PATH")
            self.connect = lambda: sqlite3.connect(path)
            self.placeholder = "?"
        with closing(self.connect()) as conn, conn, closing(conn.cursor()) as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS agent_conversations_v1 (
                conversation_id TEXT PRIMARY KEY, owner_key TEXT NOT NULL,
                revision INTEGER NOT NULL, history TEXT NOT NULL
            )""")

    def list_ids(self, owner: str) -> list[str]:
        """Return this owner's conversation IDs in ID order, without loading message history."""
        with closing(self.connect()) as conn, conn, closing(conn.cursor()) as cursor:
            cursor.execute(f"SELECT conversation_id FROM agent_conversations_v1 WHERE owner_key = {self.placeholder} "
                           "ORDER BY conversation_id", (owner,))
            return [row[0] for row in cursor.fetchall()]

    def load(self, owner: str, conversation_id: str) -> tuple[int, str] | None:
        """Return (revision, serialized history), or None if the owner has no such conversation."""
        param = self.placeholder
        with closing(self.connect()) as conn, conn, closing(conn.cursor()) as cursor:
            cursor.execute(f"SELECT revision, history FROM agent_conversations_v1 "
                           f"WHERE conversation_id = {param} AND owner_key = {param}", (conversation_id, owner))
            return cursor.fetchone()

    def save(self, owner: str, conversation_id: str, revision: int, history: str) -> bool:
        """Persist the complete serialized history using the caller's last known revision.

        Revision zero inserts a new conversation at revision one; a duplicate ID
        raises a database integrity error. An update advances the revision only
        if the ID, owner, and revision still match. Return whether one row was written.
        """
        param = self.placeholder
        with closing(self.connect()) as conn, conn, closing(conn.cursor()) as cursor:
            if revision == 0:
                cursor.execute(f"INSERT INTO agent_conversations_v1 (conversation_id, owner_key, revision, history) "
                               f"VALUES ({param}, {param}, 1, {param})", (conversation_id, owner, history))
            else:
                cursor.execute(f"UPDATE agent_conversations_v1 SET history = {param}, revision = revision + 1 "
                               f"WHERE conversation_id = {param} AND owner_key = {param} AND revision = {param}",
                               (history, conversation_id, owner, revision))
            return cursor.rowcount == 1
