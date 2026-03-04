"""Shared incremental sync logic.

Used by both the cron-based incremental script and the RTM daemon's
catch-up mechanism to fill gaps after being offline.
"""

import gc
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from slack_exporter.client import SlackClient, SlackAPIError, SlackMessage
from slack_exporter.config import WorkspaceConfig
from slack_exporter.storage import Storage, Channel, User, Message, Attachment


def parse_slack_ts(ts: str) -> datetime:
    """Parse Slack timestamp to datetime."""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def _store_messages(
    client: SlackClient,
    storage: Storage,
    workspace: str,
    channel_id: str,
    messages: list[SlackMessage],
    seen_ts: set[str],
    user_ids: set[str],
    attachments_dir: Path | None,
) -> tuple[int, int]:
    """Convert a batch of SlackMessages to storage format, store, and download attachments.
    Returns (msg_count, attachment_count). Updates seen_ts and user_ids in place."""
    storage_messages = []
    for msg in messages:
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

    msg_count = storage.upsert_messages_batch(storage_messages)

    att_count = 0
    if attachments_dir:
        downloaded = client.download_files_from_messages(messages, attachments_dir, image_only=False)
        attachments = [
            Attachment(
                id=file.id,
                workspace=workspace,
                channel_id=channel_id,
                message_ts=file.message_ts,
                name=file.name,
                mimetype=file.mimetype,
                size=file.size,
                local_path=str(path),
            )
            for file, path in downloaded
        ]
        att_count = storage.upsert_attachments_batch(attachments)

    return msg_count, att_count


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
    Processes messages in pages to limit peak memory usage.
    Returns (message_count, attachment_count).
    """
    oldest_ts = storage.get_latest_message_ts(workspace, channel_id)
    oldest_float = float(oldest_ts) if oldest_ts else None

    total_msgs = 0
    total_atts = 0
    seen_ts: set[str] = set()
    user_ids: set[str] = set()
    thread_parents_to_refresh: list[str] = []

    # Paginate through channel history and store each page immediately
    latest = None
    while True:
        page_messages, next_cursor = client.get_channel_history(
            channel_id=channel_id,
            latest=latest,
        )

        # Filter out messages older than our cutoff
        if oldest_float:
            filtered = [m for m in page_messages if float(m.ts) > oldest_float]
            hit_boundary = len(filtered) < len(page_messages)
            page_messages = filtered
        else:
            hit_boundary = False

        if not page_messages:
            break

        # Track thread parents for later refresh
        for msg in page_messages:
            if msg.reply_count > 0 or (msg.thread_ts and msg.thread_ts == msg.ts):
                thread_ts = msg.thread_ts if msg.thread_ts else msg.ts
                if thread_ts not in seen_ts:
                    thread_parents_to_refresh.append(thread_ts)

        # Store this page immediately
        mc, ac = _store_messages(
            client, storage, workspace, channel_id,
            page_messages, seen_ts, user_ids, attachments_dir,
        )
        total_msgs += mc
        total_atts += ac

        if hit_boundary or not next_cursor:
            break
        latest = next_cursor

    # Fetch and store thread replies in small batches
    for thread_ts in thread_parents_to_refresh:
        try:
            replies = client.get_thread_replies(channel_id, thread_ts, oldest=oldest_ts)
            # Filter to only new replies (skip parent and already-seen)
            new_replies = [r for r in replies if r.ts not in seen_ts]
            if new_replies:
                mc, ac = _store_messages(
                    client, storage, workspace, channel_id,
                    new_replies, seen_ts, user_ids, attachments_dir,
                )
                total_msgs += mc
                total_atts += ac
        except SlackAPIError:
            pass

    # Check recently-active threads (last 3 days) for new replies
    stored_threads = storage.get_thread_parents(workspace, channel_id)
    three_days_ago = str(datetime.now(timezone.utc).timestamp() - 3 * 24 * 3600)
    recent_threads = storage.get_recently_active_threads(workspace, channel_id, three_days_ago)

    for thread_ts in recent_threads:
        if thread_ts in seen_ts:
            continue
        stored_latest = stored_threads.get(thread_ts)
        try:
            thread_msgs = client.get_thread_replies(channel_id, thread_ts)
            if not thread_msgs:
                continue
            parent = next((m for m in thread_msgs if m.ts == thread_ts), None)
            if parent and parent.latest_reply:
                if stored_latest is None or parent.latest_reply > stored_latest:
                    mc, ac = _store_messages(
                        client, storage, workspace, channel_id,
                        thread_msgs, seen_ts, user_ids, attachments_dir,
                    )
                    total_msgs += mc
                    total_atts += ac
        except SlackAPIError:
            pass

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

    return total_msgs, total_atts


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

            # Free memory between channels
            gc.collect()

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

                # Free memory between DMs
                gc.collect()
        except SlackAPIError:
            pass

    storage.update_last_sync(config.name, datetime.now(timezone.utc))
    return total_msgs, total_atts
