"""SQLite database operations for storing Slack data."""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional


@dataclass
class Channel:
    """Slack channel record."""
    id: str
    workspace: str
    name: str
    topic: Optional[str] = None
    purpose: Optional[str] = None
    member_count: Optional[int] = None
    is_dm: bool = False


@dataclass
class User:
    """Slack user record."""
    id: str
    workspace: str
    username: str
    real_name: Optional[str] = None


@dataclass
class Message:
    """Slack message record."""
    id: str  # timestamp as ID (e.g., "1767377260.138949")
    workspace: str
    channel_id: str
    user_id: Optional[str]
    text: str
    timestamp: datetime
    thread_ts: Optional[str] = None
    reactions: Optional[str] = None
    latest_reply: Optional[str] = None  # Timestamp of latest reply (for thread parents)
    blocks: Optional[str] = None  # JSON string of blocks/attachments for rich content


@dataclass
class Attachment:
    """File attachment record."""
    id: str
    workspace: str
    channel_id: str
    message_ts: str
    name: str
    mimetype: str
    size: int
    local_path: Optional[str] = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    name TEXT PRIMARY KEY,
    last_sync TEXT
);

CREATE TABLE IF NOT EXISTS channels (
    id TEXT NOT NULL,
    workspace TEXT NOT NULL,
    name TEXT NOT NULL,
    topic TEXT,
    purpose TEXT,
    member_count INTEGER,
    is_dm INTEGER DEFAULT 0,
    PRIMARY KEY (id, workspace),
    FOREIGN KEY (workspace) REFERENCES workspaces(name)
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT NOT NULL,
    workspace TEXT NOT NULL,
    username TEXT NOT NULL,
    real_name TEXT,
    PRIMARY KEY (id, workspace),
    FOREIGN KEY (workspace) REFERENCES workspaces(name)
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT NOT NULL,
    workspace TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    user_id TEXT,
    text TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    thread_ts TEXT,
    reactions TEXT,
    latest_reply TEXT,
    blocks TEXT,
    PRIMARY KEY (id, workspace, channel_id),
    FOREIGN KEY (workspace) REFERENCES workspaces(name),
    FOREIGN KEY (channel_id, workspace) REFERENCES channels(id, workspace)
);

CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, workspace);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_ts);

CREATE TABLE IF NOT EXISTS attachments (
    id TEXT NOT NULL,
    workspace TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_ts TEXT NOT NULL,
    name TEXT NOT NULL,
    mimetype TEXT,
    size INTEGER,
    local_path TEXT,
    PRIMARY KEY (id, workspace),
    FOREIGN KEY (workspace) REFERENCES workspaces(name)
);

CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_ts, channel_id, workspace);

CREATE TABLE IF NOT EXISTS rate_limits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    workspace TEXT NOT NULL,
    method TEXT NOT NULL,
    retry_after INTEGER,
    attempt INTEGER
);

CREATE INDEX IF NOT EXISTS idx_rate_limits_timestamp ON rate_limits(timestamp);
CREATE INDEX IF NOT EXISTS idx_rate_limits_workspace ON rate_limits(workspace);
"""


class Storage:
    """SQLite storage for Slack data."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        """Initialize database schema."""
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # Migration: add blocks column if it doesn't exist
            try:
                conn.execute("ALTER TABLE messages ADD COLUMN blocks TEXT")
            except Exception:
                pass  # Column already exists

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_workspace(self, name: str) -> None:
        """Create or update a workspace."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO workspaces (name) VALUES (?)",
                (name,)
            )

    def update_last_sync(self, workspace: str, timestamp: datetime) -> None:
        """Update the last sync time for a workspace."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE workspaces SET last_sync = ? WHERE name = ?",
                (timestamp.isoformat(), workspace)
            )

    def get_last_sync(self, workspace: str) -> Optional[datetime]:
        """Get the last sync time for a workspace."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_sync FROM workspaces WHERE name = ?",
                (workspace,)
            ).fetchone()
            if row and row["last_sync"]:
                return datetime.fromisoformat(row["last_sync"])
            return None

    def upsert_channel(self, channel: Channel) -> None:
        """Create or update a channel."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO channels (id, workspace, name, topic, purpose, member_count, is_dm)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id, workspace) DO UPDATE SET
                    name = excluded.name,
                    topic = excluded.topic,
                    purpose = excluded.purpose,
                    member_count = excluded.member_count,
                    is_dm = excluded.is_dm
                """,
                (channel.id, channel.workspace, channel.name, channel.topic,
                 channel.purpose, channel.member_count, 1 if channel.is_dm else 0)
            )

    def upsert_user(self, user: User) -> None:
        """Create or update a user."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (id, workspace, username, real_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (id, workspace) DO UPDATE SET
                    username = excluded.username,
                    real_name = excluded.real_name
                """,
                (user.id, user.workspace, user.username, user.real_name)
            )

    def upsert_message(self, message: Message) -> None:
        """Create or update a message."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (id, workspace, channel_id, user_id, text, timestamp, thread_ts, reactions, latest_reply, blocks)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id, workspace, channel_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    text = excluded.text,
                    timestamp = excluded.timestamp,
                    thread_ts = excluded.thread_ts,
                    reactions = excluded.reactions,
                    latest_reply = excluded.latest_reply,
                    blocks = excluded.blocks
                """,
                (message.id, message.workspace, message.channel_id, message.user_id,
                 message.text, message.timestamp.isoformat(), message.thread_ts, message.reactions,
                 message.latest_reply, message.blocks)
            )

    def upsert_messages_batch(self, messages: list[Message]) -> int:
        """Batch insert/update messages. Returns count of messages processed."""
        if not messages:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO messages (id, workspace, channel_id, user_id, text, timestamp, thread_ts, reactions, latest_reply, blocks)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id, workspace, channel_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    text = excluded.text,
                    timestamp = excluded.timestamp,
                    thread_ts = excluded.thread_ts,
                    reactions = excluded.reactions,
                    latest_reply = excluded.latest_reply,
                    blocks = excluded.blocks
                """,
                [(m.id, m.workspace, m.channel_id, m.user_id, m.text,
                  m.timestamp.isoformat(), m.thread_ts, m.reactions, m.latest_reply, m.blocks) for m in messages]
            )
        return len(messages)

    def get_latest_message_ts(self, workspace: str, channel_id: str) -> Optional[str]:
        """Get the timestamp of the latest message in a channel."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM messages
                WHERE workspace = ? AND channel_id = ?
                ORDER BY timestamp DESC LIMIT 1
                """,
                (workspace, channel_id)
            ).fetchone()
            return row["id"] if row else None

    def get_message_count(self, workspace: str, channel_id: Optional[str] = None) -> int:
        """Get count of messages in a workspace/channel."""
        with self._connect() as conn:
            if channel_id:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM messages WHERE workspace = ? AND channel_id = ?",
                    (workspace, channel_id)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM messages WHERE workspace = ?",
                    (workspace,)
                ).fetchone()
            return row["cnt"] if row else 0

    def get_thread_parents(self, workspace: str, channel_id: str) -> dict[str, Optional[str]]:
        """Get all thread parent message IDs and their latest_reply timestamps."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, latest_reply FROM messages
                WHERE workspace = ? AND channel_id = ? AND thread_ts = id
                """,
                (workspace, channel_id)
            ).fetchall()
            return {row["id"]: row["latest_reply"] for row in rows}

    def get_recently_active_threads(self, workspace: str, channel_id: str, since_ts: str) -> list[str]:
        """Get thread_ts values for threads with recent replies (based on stored latest_reply)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM messages
                WHERE workspace = ? AND channel_id = ?
                AND thread_ts = id
                AND latest_reply IS NOT NULL
                AND latest_reply > ?
                """,
                (workspace, channel_id, since_ts)
            ).fetchall()
            return [row["id"] for row in rows]

    def upsert_attachment(self, attachment: Attachment) -> None:
        """Create or update an attachment record."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO attachments (id, workspace, channel_id, message_ts, name, mimetype, size, local_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id, workspace) DO UPDATE SET
                    local_path = excluded.local_path
                """,
                (attachment.id, attachment.workspace, attachment.channel_id, attachment.message_ts,
                 attachment.name, attachment.mimetype, attachment.size, attachment.local_path)
            )

    def upsert_attachments_batch(self, attachments: list[Attachment]) -> int:
        """Batch insert/update attachments. Returns count processed."""
        if not attachments:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO attachments (id, workspace, channel_id, message_ts, name, mimetype, size, local_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id, workspace) DO UPDATE SET
                    local_path = excluded.local_path
                """,
                [(a.id, a.workspace, a.channel_id, a.message_ts, a.name, a.mimetype, a.size, a.local_path)
                 for a in attachments]
            )
        return len(attachments)

    def log_rate_limit(self, workspace: str, method: str, retry_after: int, attempt: int) -> None:
        """Log a rate limit event."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rate_limits (timestamp, workspace, method, retry_after, attempt)
                VALUES (?, ?, ?, ?, ?)
                """,
                (datetime.now(timezone.utc).isoformat(), workspace, method, retry_after, attempt)
            )

    def get_rate_limit_stats(self, hours: int = 24) -> dict:
        """Get rate limit statistics for the last N hours."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._connect() as conn:
            # Total count
            total = conn.execute(
                "SELECT COUNT(*) as cnt FROM rate_limits WHERE timestamp > ?",
                (since,)
            ).fetchone()["cnt"]

            # By workspace
            by_workspace = conn.execute(
                """
                SELECT workspace, COUNT(*) as cnt
                FROM rate_limits WHERE timestamp > ?
                GROUP BY workspace ORDER BY cnt DESC
                """,
                (since,)
            ).fetchall()

            # By method
            by_method = conn.execute(
                """
                SELECT method, COUNT(*) as cnt
                FROM rate_limits WHERE timestamp > ?
                GROUP BY method ORDER BY cnt DESC
                """,
                (since,)
            ).fetchall()

            # By hour
            by_hour = conn.execute(
                """
                SELECT strftime('%Y-%m-%d %H:00', timestamp) as hour, COUNT(*) as cnt
                FROM rate_limits WHERE timestamp > ?
                GROUP BY hour ORDER BY hour DESC LIMIT 24
                """,
                (since,)
            ).fetchall()

            return {
                "total": total,
                "by_workspace": [(r["workspace"], r["cnt"]) for r in by_workspace],
                "by_method": [(r["method"], r["cnt"]) for r in by_method],
                "by_hour": [(r["hour"], r["cnt"]) for r in by_hour],
            }
