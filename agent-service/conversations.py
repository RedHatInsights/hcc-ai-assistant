import os
import sqlite3
from contextlib import closing


class Conversations:
    def __init__(self):
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

    def load(self, owner: str, conversation_id: str) -> tuple[int, str] | None:
        param = self.placeholder
        with closing(self.connect()) as conn, conn, closing(conn.cursor()) as cursor:
            cursor.execute(f"SELECT revision, history FROM agent_conversations_v1 "
                           f"WHERE conversation_id = {param} AND owner_key = {param}", (conversation_id, owner))
            return cursor.fetchone()

    def save(self, owner: str, conversation_id: str, revision: int, history: str) -> bool:
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
