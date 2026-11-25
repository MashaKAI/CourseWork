import sqlite3
from pathlib import Path
from typing import Any


class StorageError(Exception):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    tag TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_bot INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS blocks (
    user_id TEXT NOT NULL,
    blocked_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (user_id, blocked_id)
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    admin_id TEXT
);
CREATE TABLE IF NOT EXISTS participants (
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    last_read TEXT,
    PRIMARY KEY (conversation_id, user_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    image TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages (conversation_id, created_at);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL
);
"""

TABLES = ("users", "blocks", "conversations", "participants", "messages", "sessions")


class SqliteStorage:
    """Stores the dataset in a SQLite database. Every save is a single transaction."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "is_bot" not in columns:  # database created before bots existed
            conn.execute("ALTER TABLE users ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        return conn

    def load(self) -> dict[str, Any]:
        try:
            conn = self._connect()
            try:
                blocked: dict[str, list[str]] = {}
                for r in conn.execute("SELECT * FROM blocks ORDER BY position"):
                    blocked.setdefault(r["user_id"], []).append(r["blocked_id"])
                users = [
                    {**dict(r), "is_bot": bool(r["is_bot"]), "blocked": blocked.get(r["id"], [])}
                    for r in conn.execute("SELECT * FROM users")
                ]

                parts: dict[str, list[sqlite3.Row]] = {}
                for r in conn.execute("SELECT * FROM participants ORDER BY position"):
                    parts.setdefault(r["conversation_id"], []).append(r)
                conversations = []
                for r in conn.execute("SELECT * FROM conversations"):
                    rows = parts.get(r["id"], [])
                    reads = {p["user_id"]: p["last_read"] for p in rows if p["last_read"]}
                    conversations.append(
                        {
                            "id": r["id"],
                            "title": r["title"],
                            "admin_id": r["admin_id"],
                            "participants": [p["user_id"] for p in rows],
                            "reads": reads or None,
                        }
                    )

                messages = [
                    dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY created_at")
                ]
                sessions = {
                    r["token_hash"]: r["user_id"] for r in conn.execute("SELECT * FROM sessions")
                }
            finally:
                conn.close()
        except sqlite3.DatabaseError as e:
            raise StorageError(f"База даних пошкоджена: {self.path}") from e
        return {
            "users": users,
            "conversations": conversations,
            "messages": messages,
            "sessions": sessions,
        }

    def save(self, data: dict[str, Any]) -> None:
        try:
            conn = self._connect()
            try:
                with conn:  # one transaction: commit on success, rollback on error
                    for table in TABLES:
                        conn.execute(f"DELETE FROM {table}")
                    conn.executemany(
                        "INSERT INTO users (id, username, tag, password_hash, is_bot) VALUES (?, ?, ?, ?, ?)",
                        [
                            (u["id"], u["username"], u["tag"], u["password_hash"], int(u["is_bot"]))
                            for u in data["users"]
                        ],
                    )
                    conn.executemany(
                        "INSERT INTO blocks (user_id, blocked_id, position) VALUES (?, ?, ?)",
                        [
                            (u["id"], b, i)
                            for u in data["users"]
                            for i, b in enumerate(u["blocked"])
                        ],
                    )
                    conn.executemany(
                        "INSERT INTO conversations (id, title, admin_id) VALUES (?, ?, ?)",
                        [(c["id"], c["title"], c["admin_id"]) for c in data["conversations"]],
                    )
                    conn.executemany(
                        "INSERT INTO participants (conversation_id, user_id, position, last_read)"
                        " VALUES (?, ?, ?, ?)",
                        [
                            (c["id"], p, i, (c["reads"] or {}).get(p))
                            for c in data["conversations"]
                            for i, p in enumerate(c["participants"])
                        ],
                    )
                    conn.executemany(
                        "INSERT INTO messages (id, conversation_id, sender_id, text, created_at, image)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        [
                            (m["id"], m["conversation_id"], m["sender_id"], m["text"],
                             m["created_at"], m["image"])
                            for m in data["messages"]
                        ],
                    )
                    conn.executemany(
                        "INSERT INTO sessions (token_hash, user_id) VALUES (?, ?)",
                        list(data["sessions"].items()),
                    )
            finally:
                conn.close()
        except sqlite3.DatabaseError as e:
            raise StorageError(f"Не вдалося зберегти дані: {self.path}") from e
