#!/usr/bin/env python3
"""
Incremental sync script for cron.

Syncs messages since the last-seen timestamp per channel. If your PC is
offline for any period, the next run will catch up on all missed messages.

Usage:
    # Add to crontab (every 15 minutes):
    # */15 * * * * cd /path/to/slack-exporter && /full/path/to/uv run python scripts/incremental.py >> /tmp/slack-sync.log 2>&1

    # Or run manually:
    uv run python scripts/incremental.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from slack_exporter.client import SlackClient, SlackAPIError
from slack_exporter.config import get_workspaces, get_db_path, get_attachments_dir
from slack_exporter.storage import Storage, Channel, User, Message, Attachment


def parse_slack_ts(ts: str) -> datetime:
    """Parse Slack timestamp to datetime."""
    unix_ts = float(ts)
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc)


def sync_channel_incremental(
    client: SlackClient,
    storage: Storage,
    workspace: str,
    channel_id: str,
    channel_name: str,
    is_dm: bool = False,
    attachments_dir: Path | None = None,
) -> tuple[int, int]:
    """
    Incrementally sync a channel from the last known message.
    Returns (message_count, attachment_count).
    """
    # Get latest message timestamp for incremental sync
    oldest_ts = storage.get_latest_message_ts(workspace, channel_id)

    # Fetch new messages
    messages = client.get_all_channel_messages(channel_id, oldest=oldest_ts)

    if not messages:
        return 0, 0

    # Convert to storage format
    storage_messages = []
    user_ids = set()

    for msg in messages:
        if msg.user_id:
            user_ids.add(msg.user_id)

        storage_messages.append(Message(
            id=msg.ts,
            workspace=workspace,
            channel_id=channel_id,
            user_id=msg.user_id,
            text=msg.text,
            timestamp=parse_slack_ts(msg.ts),
            thread_ts=msg.thread_ts,
            reactions=msg.reactions,
        ))

    # Fetch user info for new users
    for user_id in user_ids:
        user = client.get_user_info(user_id)
        if user:
            storage.upsert_user(User(
                id=user.id,
                workspace=workspace,
                username=user.username,
                real_name=user.real_name,
            ))

    # Store messages
    msg_count = storage.upsert_messages_batch(storage_messages)

    # Download attachments
    attachment_count = 0
    if attachments_dir:
        downloaded = client.download_files_from_messages(messages, attachments_dir, image_only=True)
        attachments = []
        for file, path in downloaded:
            attachments.append(Attachment(
                id=file.id,
                workspace=workspace,
                channel_id=channel_id,
                message_ts=file.message_ts,
                name=file.name,
                mimetype=file.mimetype,
                size=file.size,
                local_path=str(path),
            ))
        attachment_count = storage.upsert_attachments_batch(attachments)

    return msg_count, attachment_count


def main():
    now = datetime.now(timezone.utc)
    print(f"[{now.isoformat()}] Starting incremental sync...")

    db_path = get_db_path()
    attachments_dir = get_attachments_dir()
    storage = Storage(db_path)
    workspaces = get_workspaces()

    total_msgs = 0
    total_atts = 0

    for ws in workspaces:
        storage.upsert_workspace(ws.name)

        with SlackClient(ws.xoxc_token, ws.xoxd_token, workspace=ws.subdomain) as client:
            # Sync channels
            for channel_name in ws.channels:
                try:
                    channel = client.get_channel_by_name(channel_name)
                    if not channel:
                        continue

                    msg_count, att_count = sync_channel_incremental(
                        client=client,
                        storage=storage,
                        workspace=ws.name,
                        channel_id=channel.id,
                        channel_name=channel.name,
                        attachments_dir=attachments_dir,
                    )
                    if msg_count > 0:
                        print(f"  {ws.name}/#{channel_name}: {msg_count} msgs, {att_count} atts")
                    total_msgs += msg_count
                    total_atts += att_count
                except SlackAPIError as e:
                    print(f"  Error syncing {ws.name}/#{channel_name}: {e}")

            # Sync DMs
            try:
                dms = client.list_dms()
                for dm in dms:
                    try:
                        msg_count, att_count = sync_channel_incremental(
                            client=client,
                            storage=storage,
                            workspace=ws.name,
                            channel_id=dm.id,
                            channel_name=dm.name,
                            is_dm=True,
                            attachments_dir=attachments_dir,
                        )
                        if msg_count > 0:
                            print(f"  {ws.name}/{dm.name}: {msg_count} msgs, {att_count} atts")
                        total_msgs += msg_count
                        total_atts += att_count
                    except SlackAPIError:
                        pass
            except SlackAPIError:
                pass

        storage.update_last_sync(ws.name, now)

    print(f"[{now.isoformat()}] Complete: {total_msgs} msgs, {total_atts} atts")


if __name__ == "__main__":
    main()
