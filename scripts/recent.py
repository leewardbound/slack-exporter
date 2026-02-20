#!/usr/bin/env python3
"""
Show recent/ongoing conversations (messages in last 24h) with latest messages.

Usage:
    uv run python scripts/recent.py                    # All workspaces
    uv run python scripts/recent.py --workspace mycompany  # Specific workspace
    uv run python scripts/recent.py --hours 48         # Last 48 hours
    uv run python scripts/recent.py --messages 10      # Show 10 messages per channel
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from slack_exporter.config import get_db_path


def get_active_channels(conn: sqlite3.Connection, since: datetime, workspace: str | None = None) -> list[dict]:
    """Get channels with messages since the given time."""
    query = """
        SELECT DISTINCT
            c.id,
            c.workspace,
            c.name,
            c.is_dm,
            MAX(m.timestamp) as last_message_time,
            COUNT(m.id) as recent_count
        FROM channels c
        JOIN messages m ON m.channel_id = c.id AND m.workspace = c.workspace
        WHERE m.timestamp >= ?
    """
    params = [since.isoformat()]

    if workspace:
        query += " AND c.workspace = ?"
        params.append(workspace)

    query += " GROUP BY c.id, c.workspace ORDER BY last_message_time DESC"

    cursor = conn.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def get_latest_messages(
    conn: sqlite3.Connection,
    workspace: str,
    channel_id: str,
    limit: int = 5
) -> list[dict]:
    """Get the latest N messages from a channel."""
    cursor = conn.execute("""
        SELECT
            m.id,
            m.text,
            m.timestamp,
            m.thread_ts,
            m.blocks,
            u.username,
            u.real_name
        FROM messages m
        LEFT JOIN users u ON m.user_id = u.id AND m.workspace = u.workspace
        WHERE m.workspace = ? AND m.channel_id = ?
        ORDER BY m.timestamp DESC
        LIMIT ?
    """, (workspace, channel_id, limit))
    return [dict(row) for row in cursor.fetchall()]


def extract_text_from_blocks(blocks_json: str) -> str:
    """Extract readable text from Slack blocks/attachments JSON."""
    import json
    try:
        data = json.loads(blocks_json)
    except (json.JSONDecodeError, TypeError):
        return ""

    texts = []

    # Extract from blocks
    for block in data.get("blocks", []):
        if block.get("type") == "section":
            if "text" in block and block["text"].get("text"):
                texts.append(block["text"]["text"])
        elif block.get("type") == "rich_text":
            for elem in block.get("elements", []):
                for sub in elem.get("elements", []):
                    if sub.get("type") == "text":
                        texts.append(sub.get("text", ""))

    # Extract from attachments (legacy Slack format)
    for att in data.get("attachments", []):
        if att.get("title"):
            texts.append(att["title"])
        if att.get("text"):
            texts.append(att["text"])
        if att.get("fallback"):
            texts.append(att["fallback"])

    return " | ".join(filter(None, texts[:3]))  # First 3 text pieces


def format_timestamp(ts_str: str) -> str:
    """Format ISO timestamp for display."""
    dt = datetime.fromisoformat(ts_str)
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    diff = now - dt
    if diff < timedelta(hours=1):
        mins = int(diff.total_seconds() / 60)
        return f"{mins}m ago"
    elif diff < timedelta(hours=24):
        hours = int(diff.total_seconds() / 3600)
        return f"{hours}h ago"
    else:
        return dt.strftime("%Y-%m-%d %H:%M")


def truncate_text(text: str, max_len: int = 100) -> str:
    """Truncate text with ellipsis."""
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def main():
    parser = argparse.ArgumentParser(description="Show recent Slack conversations")
    parser.add_argument(
        "--workspace", "-w",
        help="Filter to specific workspace",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Show channels with messages in last N hours (default: 24)",
    )
    parser.add_argument(
        "--messages", "-n",
        type=int,
        default=5,
        help="Number of messages to show per channel (default: 5)",
    )
    args = parser.parse_args()

    db_path = get_db_path()
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        print("Run sync.py first to populate the database.")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)

    channels = get_active_channels(conn, since, args.workspace)

    if not channels:
        scope = f" in {args.workspace}" if args.workspace else ""
        print(f"No active conversations{scope} in the last {args.hours} hours.")
        sys.exit(0)

    print(f"=== Active conversations (last {args.hours}h) ===\n")

    for ch in channels:
        prefix = "DM" if ch["is_dm"] else "#"
        print(f"{ch['workspace']}/{prefix}{ch['name']} ({ch['recent_count']} recent)")
        print("-" * 60)

        messages = get_latest_messages(conn, ch["workspace"], ch["id"], args.messages)

        # Reverse to show oldest first (chronological)
        for msg in reversed(messages):
            user = msg["username"] or "unknown"
            time_str = format_timestamp(msg["timestamp"])
            # Use text if available, otherwise extract from blocks
            text = msg["text"]
            if not text and msg["blocks"]:
                text = extract_text_from_blocks(msg["blocks"])
            text = truncate_text(text or "[no text]")
            thread_marker = " [thread]" if msg["thread_ts"] and msg["thread_ts"] != msg["id"] else ""
            print(f"  [{time_str}] {user}: {text}{thread_marker}")

        print()

    conn.close()


if __name__ == "__main__":
    main()
