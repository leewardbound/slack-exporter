#!/usr/bin/env python3
"""
Show active topics - threads with messages in the last 24h.
Shows first 5 messages (context) and last 20 messages (recent) for each thread.

Usage:
    uv run python scripts/active_topics.py
    uv run python scripts/active_topics.py -w mycompany
    uv run python scripts/active_topics.py --hours 48
    uv run python scripts/active_topics.py --json
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from slack_exporter.config import get_db_path
from slack_exporter.storage import Storage


def get_active_threads(storage: Storage, hours: int = 24, workspace: str = None):
    """Find threads with messages in the last N hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_ts = str(cutoff.timestamp())

    with storage._connect() as conn:
        if workspace:
            rows = conn.execute(
                """
                SELECT DISTINCT m.thread_ts, m.workspace, m.channel_id,
                       c.name as channel_name,
                       MAX(m.timestamp) as latest_activity,
                       COUNT(*) as recent_count
                FROM messages m
                LEFT JOIN channels c ON m.channel_id = c.id AND m.workspace = c.workspace
                WHERE m.thread_ts IS NOT NULL
                  AND m.timestamp > ?
                  AND m.workspace = ?
                GROUP BY m.thread_ts, m.workspace, m.channel_id, c.name
                ORDER BY latest_activity DESC
                """,
                (cutoff_ts, workspace)
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT m.thread_ts, m.workspace, m.channel_id,
                       c.name as channel_name,
                       MAX(m.timestamp) as latest_activity,
                       COUNT(*) as recent_count
                FROM messages m
                LEFT JOIN channels c ON m.channel_id = c.id AND m.workspace = c.workspace
                WHERE m.thread_ts IS NOT NULL
                  AND m.timestamp > ?
                GROUP BY m.thread_ts, m.workspace, m.channel_id, c.name
                ORDER BY latest_activity DESC
                """,
                (cutoff_ts,)
            ).fetchall()
        return [dict(row) for row in rows]


def get_thread_summary(storage: Storage, thread_ts: str, workspace: str):
    """Get first 5 and last 20 messages from a thread."""
    with storage._connect() as conn:
        # Get total count
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE thread_ts = ? AND workspace = ?",
            (thread_ts, workspace)
        ).fetchone()["cnt"]

        # Get first 5 messages
        first_rows = conn.execute(
            """
            SELECT m.id, m.timestamp, m.text, m.user_id,
                   u.username, u.real_name
            FROM messages m
            LEFT JOIN users u ON m.user_id = u.id AND m.workspace = u.workspace
            WHERE m.thread_ts = ? AND m.workspace = ?
            ORDER BY m.timestamp ASC
            LIMIT 5
            """,
            (thread_ts, workspace)
        ).fetchall()

        # Get last 20 messages
        last_rows = conn.execute(
            """
            SELECT m.id, m.timestamp, m.text, m.user_id,
                   u.username, u.real_name
            FROM messages m
            LEFT JOIN users u ON m.user_id = u.id AND m.workspace = u.workspace
            WHERE m.thread_ts = ? AND m.workspace = ?
            ORDER BY m.timestamp DESC
            LIMIT 20
            """,
            (thread_ts, workspace)
        ).fetchall()

        # Reverse last messages to chronological order
        last_rows = list(reversed(last_rows))

        return {
            "total_messages": total,
            "first_messages": [dict(r) for r in first_rows],
            "last_messages": [dict(r) for r in last_rows],
        }


def format_message(msg: dict, indent: str = "  ") -> str:
    """Format a message for display."""
    ts = msg["timestamp"]
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        time_str = dt.strftime("%m-%d %H:%M")
    except:
        time_str = ts[:10]

    author = msg["real_name"] or msg["username"] or msg["user_id"] or "unknown"
    text = msg["text"].replace("\n", " ")
    text = text[:120] + "..." if len(text) > 120 else text

    return f"{indent}{time_str} | {author}: {text}"


def format_thread(thread: dict, summary: dict) -> str:
    """Format a thread for display."""
    lines = []

    # Header
    workspace = thread["workspace"]
    channel = thread["channel_name"] or thread["channel_id"]
    total = summary["total_messages"]
    thread_ts = thread["thread_ts"]

    # Get parent message text (first message)
    parent = summary["first_messages"][0] if summary["first_messages"] else None
    parent_preview = ""
    if parent:
        parent_preview = parent["text"].replace("\n", " ")[:80]
        if len(parent["text"]) > 80:
            parent_preview += "..."

    lines.append(f"[{workspace}/#{channel}] {parent_preview}")
    lines.append(f"  Thread: {thread_ts} ({total} messages)")

    # First messages (context)
    if summary["first_messages"]:
        lines.append("  --- First messages ---")
        for msg in summary["first_messages"]:
            lines.append(format_message(msg))

    # Show gap if there are more messages
    first_ids = {m["id"] for m in summary["first_messages"]}
    last_unique = [m for m in summary["last_messages"] if m["id"] not in first_ids]

    if total > 25 and last_unique:
        gap = total - len(summary["first_messages"]) - len(last_unique)
        if gap > 0:
            lines.append(f"  ... {gap} more messages ...")

    # Last messages (recent)
    if last_unique:
        lines.append("  --- Recent messages ---")
        for msg in last_unique:
            lines.append(format_message(msg))

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Show active topics (threads with recent activity)")
    parser.add_argument("-w", "--workspace", help="Filter to specific workspace")
    parser.add_argument("--hours", type=int, default=24, help="Look back N hours (default: 24)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--limit", type=int, default=20, help="Max threads to show (default: 20)")
    args = parser.parse_args()

    storage = Storage(get_db_path())

    # Find active threads
    threads = get_active_threads(storage, args.hours, args.workspace)

    if not threads:
        print(f"No active threads in the last {args.hours} hours", file=sys.stderr)
        sys.exit(0)

    # Limit threads
    threads = threads[:args.limit]

    # Get summaries for each thread
    results = []
    for thread in threads:
        summary = get_thread_summary(storage, thread["thread_ts"], thread["workspace"])
        results.append({
            "thread": thread,
            "summary": summary,
        })

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print(f"Active Topics ({len(threads)} threads with activity in last {args.hours}h)")
        print("=" * 80)

        for i, result in enumerate(results):
            if i > 0:
                print()
                print("-" * 80)
            print(format_thread(result["thread"], result["summary"]))

        print("=" * 80)


if __name__ == "__main__":
    main()
