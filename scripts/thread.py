#!/usr/bin/env python3
"""
Get full thread context for a message or thread_ts.

Usage:
    uv run python scripts/thread.py 1736000000.123456
    uv run python scripts/thread.py 1736000000.123456 -w mycompany
    uv run python scripts/thread.py 1736000000.123456 --json
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from slack_exporter.config import get_db_path
from slack_exporter.storage import Storage


def get_thread_messages(storage: Storage, thread_ts: str, workspace: str = None):
    """Get all messages in a thread."""
    with storage._connect() as conn:
        if workspace:
            rows = conn.execute(
                """
                SELECT m.id, m.timestamp, m.text, m.thread_ts, m.user_id,
                       u.username, u.real_name, c.name as channel_name, m.workspace
                FROM messages m
                LEFT JOIN users u ON m.user_id = u.id AND m.workspace = u.workspace
                LEFT JOIN channels c ON m.channel_id = c.id AND m.workspace = c.workspace
                WHERE m.thread_ts = ? AND m.workspace = ?
                ORDER BY m.timestamp ASC
                """,
                (thread_ts, workspace)
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT m.id, m.timestamp, m.text, m.thread_ts, m.user_id,
                       u.username, u.real_name, c.name as channel_name, m.workspace
                FROM messages m
                LEFT JOIN users u ON m.user_id = u.id AND m.workspace = u.workspace
                LEFT JOIN channels c ON m.channel_id = c.id AND m.workspace = c.workspace
                WHERE m.thread_ts = ?
                ORDER BY m.timestamp ASC
                """,
                (thread_ts,)
            ).fetchall()
        return [dict(row) for row in rows]


def find_thread_for_message(storage: Storage, message_ts: str, workspace: str = None):
    """Find the thread_ts for a given message timestamp."""
    with storage._connect() as conn:
        if workspace:
            row = conn.execute(
                "SELECT thread_ts FROM messages WHERE id = ? AND workspace = ?",
                (message_ts, workspace)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT thread_ts FROM messages WHERE id = ?",
                (message_ts,)
            ).fetchone()
        return row["thread_ts"] if row else None


def format_message(msg: dict, is_parent: bool = False) -> str:
    """Format a message for display."""
    ts = msg["timestamp"]
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        time_str = dt.strftime("%Y-%m-%d %H:%M")
    except:
        time_str = ts[:16]

    author = msg["real_name"] or msg["username"] or msg["user_id"] or "unknown"
    text = msg["text"][:200] + "..." if len(msg["text"]) > 200 else msg["text"]

    prefix = "[PARENT] " if is_parent else "         "
    return f"{prefix}{time_str} | {author}: {text}"


def main():
    parser = argparse.ArgumentParser(description="Get full thread context for a message")
    parser.add_argument("message_ts", help="Message timestamp or thread_ts (e.g., 1736000000.123456)")
    parser.add_argument("-w", "--workspace", help="Filter to specific workspace")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    storage = Storage(get_db_path())

    # First, try to find the thread_ts for this message
    thread_ts = find_thread_for_message(storage, args.message_ts, args.workspace)

    # If not found as a message, assume it's already a thread_ts
    if not thread_ts:
        thread_ts = args.message_ts

    # Get all messages in the thread
    messages = get_thread_messages(storage, thread_ts, args.workspace)

    if not messages:
        print(f"No messages found for thread {thread_ts}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(messages, indent=2, default=str))
    else:
        workspace = messages[0]["workspace"]
        channel = messages[0]["channel_name"] or "unknown"
        print(f"Thread in {workspace}/#{channel} ({len(messages)} messages)")
        print(f"Thread ID: {thread_ts}")
        print("-" * 80)

        for msg in messages:
            is_parent = msg["id"] == thread_ts
            print(format_message(msg, is_parent))

        print("-" * 80)


if __name__ == "__main__":
    main()
