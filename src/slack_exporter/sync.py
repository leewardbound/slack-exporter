"""Shared incremental sync logic.

Used by both the cron-based incremental script and the RTM daemon's
catch-up mechanism to fill gaps after being offline.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from slack_exporter.client import SlackClient, SlackAPIError
from slack_exporter.config import WorkspaceConfig
from slack_exporter.storage import Storage, Channel, User, Message, Attachment


def parse_slack_ts(ts: str) -> datetime:
    """Parse Slack timestamp to datetime."""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


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
    Also refreshes threads with new replies.
    Returns (message_count, attachment_count).
    """
    # Get latest message timestamp for incremental sync
    oldest_ts = storage.get_latest_message_ts(workspace, channel_id)

    # Fetch new messages (includes thread replies for new threads)
    messages = client.get_all_channel_messages(channel_id, oldest=oldest_ts)

    # Get stored thread parents to check for stale threads
    stored_threads = storage.get_thread_parents(workspace, channel_id)

    # Check which threads need refreshing based on latest_reply
    stale_threads = []
    for msg in messages:
        if msg.latest_reply and msg.ts == msg.thread_ts:
            stored_latest = stored_threads.get(msg.ts)
            if stored_latest is None or msg.latest_reply > stored_latest:
                stale_threads.append(msg.ts)

    # Check recently-active threads (last 3 days) for new replies
    three_days_ago = str(datetime.now(timezone.utc).timestamp() - 3 * 24 * 3600)
    recent_threads = storage.get_recently_active_threads(workspace, channel_id, three_days_ago)

    # Cache fetched replies to avoid double-fetching
    fetched_replies: dict[str, list] = {}

    for thread_ts in recent_threads:
        if thread_ts in stale_threads:
            continue
        stored_latest = stored_threads.get(thread_ts)
        try:
            thread_msgs = client.get_thread_replies(channel_id, thread_ts)
            if thread_msgs:
                parent = next((m for m in thread_msgs if m.ts == thread_ts), None)
                if parent and parent.latest_reply:
                    if stored_latest is None or parent.latest_reply > stored_latest:
                        stale_threads.append(thread_ts)
                        fetched_replies[thread_ts] = thread_msgs
        except SlackAPIError:
            pass

    # Refresh stale threads (use cached replies if available)
    thread_replies = []
    for thread_ts in set(stale_threads):
        if thread_ts in fetched_replies:
            thread_replies.extend(fetched_replies[thread_ts])
        else:
            try:
                replies = client.get_thread_replies(channel_id, thread_ts)
                thread_replies.extend(replies)
            except SlackAPIError:
                pass

    # Combine all messages
    all_messages = messages + thread_replies

    if not all_messages:
        return 0, 0

    # Convert to storage format (deduplicate by ts)
    seen_ts = set()
    storage_messages = []
    user_ids = set()

    for msg in all_messages:
        if msg.ts in seen_ts:
            continue
        seen_ts.add(msg.ts)

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
            latest_reply=msg.latest_reply,
            blocks=msg.blocks,
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
        downloaded = client.download_files_from_messages(all_messages, attachments_dir, image_only=False)
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


def sync_workspace_incremental(
    config: WorkspaceConfig,
    storage: Storage,
    attachments_dir: Path | None = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> tuple[int, int]:
    """
    Run incremental sync for a single workspace.
    Fetches only messages newer than what's already in the DB.
    Returns (total_msg_count, total_att_count).
    """
    if log_fn is None:
        log_fn = print

    total_msgs = 0
    total_atts = 0

    storage.upsert_workspace(config.name)

    with SlackClient(config.xoxc_token, config.xoxd_token, workspace=config.subdomain, storage=storage) as client:
        # Resolve channels to sync
        if config.channels == ["*"]:
            # Wildcard: list all channels from the API
            try:
                all_channels = client.list_channels()
                resolved_channels = [(ch.id, ch.name) for ch in all_channels]
                log_fn(f"  {config.name}: found {len(resolved_channels)} channels")
            except SlackAPIError as e:
                log_fn(f"  Error listing channels for {config.name}: {e}")
                resolved_channels = []
        else:
            resolved_channels = []
            for channel_name in config.channels:
                try:
                    channel = client.get_channel_by_name(channel_name)
                    if channel:
                        resolved_channels.append((channel.id, channel.name))
                except SlackAPIError as e:
                    log_fn(f"  Error looking up {config.name}/#{channel_name}: {e}")

        # Sync resolved channels
        for channel_id, channel_name in resolved_channels:
            try:
                storage.upsert_channel(Channel(
                    id=channel_id,
                    workspace=config.name,
                    name=channel_name,
                ))

                msg_count, att_count = sync_channel_incremental(
                    client=client,
                    storage=storage,
                    workspace=config.name,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    attachments_dir=attachments_dir,
                )
                if msg_count > 0:
                    log_fn(f"  {config.name}/#{channel_name}: {msg_count} msgs, {att_count} atts")
                total_msgs += msg_count
                total_atts += att_count
            except SlackAPIError as e:
                log_fn(f"  Error syncing {config.name}/#{channel_name}: {e}")

        # Sync DMs
        try:
            dms = client.list_dms()
            for dm in dms:
                try:
                    storage.upsert_channel(Channel(
                        id=dm.id,
                        workspace=config.name,
                        name=dm.name,
                        is_dm=True,
                    ))

                    msg_count, att_count = sync_channel_incremental(
                        client=client,
                        storage=storage,
                        workspace=config.name,
                        channel_id=dm.id,
                        channel_name=dm.name,
                        is_dm=True,
                        attachments_dir=attachments_dir,
                    )
                    if msg_count > 0:
                        log_fn(f"  {config.name}/{dm.name}: {msg_count} msgs, {att_count} atts")
                    total_msgs += msg_count
                    total_atts += att_count
                except SlackAPIError:
                    pass
        except SlackAPIError:
            pass

    storage.update_last_sync(config.name, datetime.now(timezone.utc))
    return total_msgs, total_atts
