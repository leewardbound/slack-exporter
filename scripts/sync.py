#!/usr/bin/env python3
"""
Sync Slack messages to local SQLite database.

Usage:
    uv run python scripts/sync.py                      # Sync all configured channels
    uv run python scripts/sync.py --days 90            # Sync last 90 days
    uv run python scripts/sync.py --no-dms             # Skip DM sync
    uv run python scripts/sync.py --no-attachments     # Skip attachment downloads
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
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


def sync_channel(
    client: SlackClient,
    storage: Storage,
    workspace: str,
    channel_id: str,
    channel_name: str,
    oldest_ts: str | None = None,
    is_dm: bool = False,
    download_attachments: bool = True,
    attachments_dir: Path | None = None,
) -> tuple[int, int]:
    """
    Sync a single channel/DM. Returns (message_count, attachment_count).
    """
    # Save channel metadata
    storage.upsert_channel(Channel(
        id=channel_id,
        workspace=workspace,
        name=channel_name,
        is_dm=is_dm,
    ))

    # Get existing message count
    existing_count = storage.get_message_count(workspace, channel_id)

    # Get latest message timestamp for incremental sync
    if oldest_ts is None:
        latest_ts = storage.get_latest_message_ts(workspace, channel_id)
        if latest_ts:
            oldest_ts = latest_ts

    # Fetch messages
    messages = client.get_all_channel_messages(channel_id, oldest=oldest_ts)

    if not messages:
        return 0, 0

    # Convert to storage format and collect user IDs
    storage_messages = []
    user_ids = set()
    all_files = []

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

        # Collect files for download
        all_files.extend(msg.files)

    # Fetch and store user info
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
    if download_attachments and attachments_dir and all_files:
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


def sync_workspace(
    storage: Storage,
    workspace_name: str,
    subdomain: str,
    xoxc_token: str,
    xoxd_token: str,
    channels: list[str],
    days: int | None = None,
    include_dms: bool = True,
    download_attachments: bool = True,
    attachments_dir: Path | None = None,
) -> tuple[int, int]:
    """Sync all channels for a workspace. Returns (total_messages, total_attachments)."""
    print(f"\n=== Syncing workspace: {workspace_name} ({subdomain}.slack.com) ===")

    storage.upsert_workspace(workspace_name)

    # Calculate oldest timestamp if days specified
    oldest_ts = None
    if days:
        oldest_dt = datetime.now(timezone.utc) - timedelta(days=days)
        oldest_ts = str(oldest_dt.timestamp())
        print(f"  Limiting to last {days} days (since {oldest_dt.date()})")

    total_msgs = 0
    total_attachments = 0

    with SlackClient(xoxc_token, xoxd_token, workspace=subdomain) as client:
        # Sync configured channels
        for channel_name in channels:
            try:
                print(f"  Syncing #{channel_name}...")
                channel = client.get_channel_by_name(channel_name)
                if not channel:
                    print(f"    Channel not found!")
                    continue

                msg_count, att_count = sync_channel(
                    client=client,
                    storage=storage,
                    workspace=workspace_name,
                    channel_id=channel.id,
                    channel_name=channel.name,
                    oldest_ts=oldest_ts,
                    download_attachments=download_attachments,
                    attachments_dir=attachments_dir,
                )
                print(f"    {msg_count} messages, {att_count} attachments")
                total_msgs += msg_count
                total_attachments += att_count

            except SlackAPIError as e:
                print(f"    Error: {e}")
            except Exception as e:
                print(f"    Unexpected error: {e}")
                raise

        # Sync DMs if requested
        if include_dms:
            print(f"  Syncing DMs...")
            try:
                dms = client.list_dms()
                print(f"    Found {len(dms)} DM conversations")

                for dm in dms:
                    try:
                        # Get a readable name for the DM
                        dm_name = dm.name
                        if dm.user_id:
                            user = client.get_user_info(dm.user_id)
                            if user:
                                dm_name = f"dm-{user.username}"
                                storage.upsert_user(User(
                                    id=user.id,
                                    workspace=workspace_name,
                                    username=user.username,
                                    real_name=user.real_name,
                                ))

                        msg_count, att_count = sync_channel(
                            client=client,
                            storage=storage,
                            workspace=workspace_name,
                            channel_id=dm.id,
                            channel_name=dm_name,
                            oldest_ts=oldest_ts,
                            is_dm=True,
                            download_attachments=download_attachments,
                            attachments_dir=attachments_dir,
                        )
                        if msg_count > 0:
                            print(f"    {dm_name}: {msg_count} messages, {att_count} attachments")
                        total_msgs += msg_count
                        total_attachments += att_count

                    except SlackAPIError as e:
                        print(f"    Error syncing {dm.name}: {e}")

            except SlackAPIError as e:
                print(f"    Error listing DMs: {e}")

    # Update last sync time
    storage.update_last_sync(workspace_name, datetime.now(timezone.utc))
    print(f"  Total: {total_msgs} messages, {total_attachments} attachments")

    return total_msgs, total_attachments


def main():
    parser = argparse.ArgumentParser(description="Sync Slack messages to SQLite")
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Sync messages from the last N days (default: 90)",
    )
    parser.add_argument(
        "--workspace",
        help="Specific workspace to sync",
    )
    parser.add_argument(
        "--no-dms",
        action="store_true",
        help="Skip syncing DMs (default: include DMs)",
    )
    parser.add_argument(
        "--no-attachments",
        action="store_true",
        help="Skip downloading image attachments",
    )
    args = parser.parse_args()

    db_path = get_db_path()
    attachments_dir = get_attachments_dir()
    print(f"Database: {db_path}")
    print(f"Attachments: {attachments_dir}")

    storage = Storage(db_path)
    workspaces = get_workspaces()

    if not workspaces:
        print("No workspaces configured! Check .env.secrets and channels.txt")
        sys.exit(1)

    if args.workspace:
        workspaces = [w for w in workspaces if w.name == args.workspace]
        if not workspaces:
            print(f"Workspace '{args.workspace}' not found in config")
            sys.exit(1)

    total_msgs = 0
    total_attachments = 0

    for ws in workspaces:
        if not ws.channels:
            print(f"\nSkipping {ws.name}: no channels configured in channels.txt")
            continue

        msgs, atts = sync_workspace(
            storage=storage,
            workspace_name=ws.name,
            subdomain=ws.subdomain,
            xoxc_token=ws.xoxc_token,
            xoxd_token=ws.xoxd_token,
            channels=ws.channels,
            days=args.days,
            include_dms=not args.no_dms,
            download_attachments=not args.no_attachments,
            attachments_dir=attachments_dir,
        )
        total_msgs += msgs
        total_attachments += atts

    print(f"\n=== Sync complete: {total_msgs} messages, {total_attachments} attachments ===")


if __name__ == "__main__":
    main()
