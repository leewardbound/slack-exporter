#!/usr/bin/env python3
"""
Incremental sync script for cron.

Syncs messages since the last-seen timestamp per channel. Also checks for
stale threads (where latest_reply has changed) and refreshes them.

Usage:
    # Add to crontab (every 15 minutes):
    # */15 * * * * cd /path/to/slack-exporter && /full/path/to/uv run python scripts/incremental.py >> /tmp/slack-sync.log 2>&1

    # Or run manually:
    uv run python scripts/incremental.py
"""

import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from slack_exporter.config import get_workspaces, get_db_path, get_attachments_dir
from slack_exporter.storage import Storage
from slack_exporter.sync import sync_workspace_incremental


def kill_prior_instances() -> int:
    """
    Kill any prior running instances of this script.
    Returns count of processes killed.
    """
    my_pid = os.getpid()
    killed = 0

    try:
        # Find all python processes running incremental.py
        result = subprocess.run(
            ["pgrep", "-f", "scripts/incremental.py"],
            capture_output=True,
            text=True,
        )
        pids = [int(pid) for pid in result.stdout.strip().split() if pid]

        for pid in pids:
            if pid != my_pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                    killed += 1
                except ProcessLookupError:
                    pass  # Already dead
                except PermissionError:
                    pass  # Not ours
    except Exception:
        pass  # pgrep not available or other error

    return killed


def main():
    # Kill any stuck prior instances (shouldn't happen with flock, but safety first)
    killed = kill_prior_instances()
    if killed > 0:
        print(f"[WARNING] Killed {killed} prior instance(s) - this shouldn't happen often!")

    now = datetime.now(timezone.utc)
    print(f"[{now.isoformat()}] Starting incremental sync...")

    db_path = get_db_path()
    attachments_dir = get_attachments_dir()
    storage = Storage(db_path)
    workspaces = get_workspaces()

    total_msgs = 0
    total_atts = 0

    for ws in workspaces:
        msg_count, att_count = sync_workspace_incremental(
            config=ws,
            storage=storage,
            attachments_dir=attachments_dir,
        )
        total_msgs += msg_count
        total_atts += att_count

    print(f"[{now.isoformat()}] Complete: {total_msgs} msgs, {total_atts} atts")


if __name__ == "__main__":
    main()
