"""Slack API client using xoxc/xoxd user session tokens."""

import json
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx


@dataclass
class SlackFile:
    """File attachment from Slack."""
    id: str
    name: str
    mimetype: str
    url_private: str
    size: int
    message_ts: str
    channel_id: str


@dataclass
class SlackMessage:
    """Raw message from Slack API."""
    ts: str  # Message timestamp (used as ID)
    user_id: Optional[str]
    username: Optional[str]
    real_name: Optional[str]
    text: str
    thread_ts: Optional[str]
    reactions: Optional[str]
    channel_id: str
    reply_count: int = 0  # Number of replies (if thread parent)
    latest_reply: Optional[str] = None  # Timestamp of latest reply
    files: list[SlackFile] = field(default_factory=list)
    blocks: Optional[str] = None  # JSON string of blocks/attachments for rich content


@dataclass
class SlackChannel:
    """Raw channel from Slack API."""
    id: str
    name: str
    topic: Optional[str]
    purpose: Optional[str]
    member_count: Optional[int]
    is_im: bool = False  # Direct message
    is_mpim: bool = False  # Multi-party DM
    user_id: Optional[str] = None  # For DMs, the other user


@dataclass
class SlackUser:
    """Raw user from Slack API."""
    id: str
    username: str
    real_name: Optional[str]


class SlackClient:
    """Slack API client using xoxc/xoxd browser session tokens."""

    # Default user agent mimicking Chrome browser
    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    )

    def __init__(self, xoxc_token: str, xoxd_token: str, workspace: str = "", storage=None):
        """
        Initialize client with user session tokens.

        Args:
            xoxc_token: User client token (xoxc-...)
            xoxd_token: User session cookie (xoxd-...)
            workspace: Optional workspace subdomain (e.g., "myworkspace")
            storage: Optional Storage instance for logging rate limits
        """
        self.xoxc_token = xoxc_token
        self.xoxd_token = xoxd_token
        self.workspace = workspace
        self.storage = storage

        # Use workspace-specific URL if provided, otherwise fall back to generic
        if workspace:
            self.base_url = f"https://{workspace}.slack.com/api"
        else:
            self.base_url = "https://slack.com/api"

        # URL-encode the xoxd token for the cookie (contains + and = chars)
        xoxd_encoded = urllib.parse.quote(xoxd_token, safe='')

        self._client = httpx.Client(
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": self.USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Cookie": f"d={xoxd_encoded}",
            },
            timeout=30.0,
        )

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _post(self, method: str, data: dict, max_retries: int = 10) -> dict:
        """Make a POST request to Slack API with rate limiting and retry."""
        # Include token in form data (not as Authorization header)
        data = {**data, "token": self.xoxc_token}

        # Rate limiting: delay between requests to avoid 429s
        time.sleep(2.0)

        for attempt in range(max_retries):
            response = self._client.post(
                f"{self.base_url}/{method}",
                data=data,
            )

            # Handle rate limiting with exponential backoff
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 5))
                wait_time = max(retry_after, 2 ** attempt)
                print(f"    Rate limited, waiting {wait_time}s...")
                # Log to storage if available
                if self.storage:
                    try:
                        self.storage.log_rate_limit(self.workspace, method, retry_after, attempt)
                    except Exception:
                        pass  # Don't fail on logging errors
                time.sleep(wait_time)
                continue

            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                error = result.get("error", "Unknown error")
                # Handle rate limit error in API response
                if error == "ratelimited":
                    wait_time = 2 ** attempt
                    print(f"    Rate limited (API), waiting {wait_time}s...")
                    if self.storage:
                        try:
                            self.storage.log_rate_limit(self.workspace, method, wait_time, attempt)
                        except Exception:
                            pass
                    time.sleep(wait_time)
                    continue
                raise SlackAPIError(f"Slack API error: {error}")
            return result

        raise SlackAPIError(f"Max retries exceeded for {method}")

    def list_channels(self, types: str = "public_channel,private_channel") -> list[SlackChannel]:
        """List all channels the user has access to."""
        channels = []
        cursor = None

        while True:
            data = {
                "types": types,
                "limit": 200,
            }
            if cursor:
                data["cursor"] = cursor

            result = self._post("conversations.list", data)

            for ch in result.get("channels", []):
                channels.append(SlackChannel(
                    id=ch["id"],
                    name=ch.get("name", ""),
                    topic=ch.get("topic", {}).get("value"),
                    purpose=ch.get("purpose", {}).get("value"),
                    member_count=ch.get("num_members"),
                ))

            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        return channels

    def get_channel_by_name(self, name: str) -> Optional[SlackChannel]:
        """Find a channel by name."""
        # Remove # prefix if present
        name = name.lstrip("#")
        channels = self.list_channels()
        for ch in channels:
            if ch.name == name:
                return ch
        return None

    def get_channel_history(
        self,
        channel_id: str,
        oldest: Optional[str] = None,
        latest: Optional[str] = None,
        limit: int = 200,
    ) -> tuple[list[SlackMessage], Optional[str]]:
        """
        Get message history for a channel.

        Returns (messages, next_cursor) tuple.
        """
        data = {
            "channel": channel_id,
            "limit": limit,
        }
        if oldest:
            data["oldest"] = oldest
        if latest:
            data["latest"] = latest

        result = self._post("conversations.history", data)

        messages = []
        for msg in result.get("messages", []):
            # Skip non-message types (join/leave, etc)
            # Allow: bot_message, file_share, thread_broadcast (also sent to channel)
            if msg.get("subtype") and msg.get("subtype") not in ("bot_message", "file_share", "thread_broadcast"):
                continue

            reactions = None
            if msg.get("reactions"):
                reactions = json.dumps(msg["reactions"])

            # Extract blocks/attachments for rich content (Sentry, etc.)
            blocks = None
            rich_content = {}
            if msg.get("blocks"):
                rich_content["blocks"] = msg["blocks"]
            if msg.get("attachments"):
                rich_content["attachments"] = msg["attachments"]
            if rich_content:
                blocks = json.dumps(rich_content)

            # Extract file attachments
            files = []
            for f in msg.get("files", []):
                if f.get("url_private"):
                    files.append(SlackFile(
                        id=f["id"],
                        name=f.get("name", f["id"]),
                        mimetype=f.get("mimetype", "application/octet-stream"),
                        url_private=f["url_private"],
                        size=f.get("size", 0),
                        message_ts=msg["ts"],
                        channel_id=channel_id,
                    ))

            messages.append(SlackMessage(
                ts=msg["ts"],
                user_id=msg.get("user"),
                username=msg.get("username"),
                real_name=None,
                text=msg.get("text", ""),
                thread_ts=msg.get("thread_ts"),
                reactions=reactions,
                channel_id=channel_id,
                reply_count=msg.get("reply_count", 0),
                latest_reply=msg.get("latest_reply"),
                files=files,
                blocks=blocks,
            ))

        # Check for pagination
        has_more = result.get("has_more", False)
        next_cursor = None
        if has_more and messages:
            # Use the oldest message timestamp for pagination
            next_cursor = messages[-1].ts

        return messages, next_cursor

    def get_thread_replies(
        self,
        channel_id: str,
        thread_ts: str,
        oldest: Optional[str] = None,
    ) -> list[SlackMessage]:
        """
        Get all replies in a thread.

        Returns all messages in the thread (including parent).
        """
        data = {
            "channel": channel_id,
            "ts": thread_ts,
            "limit": 200,
        }
        if oldest:
            data["oldest"] = oldest

        all_messages = []
        cursor = None

        while True:
            if cursor:
                data["cursor"] = cursor

            result = self._post("conversations.replies", data)

            for msg in result.get("messages", []):
                # Skip non-message types
                if msg.get("subtype") and msg.get("subtype") not in ("bot_message", "file_share", "thread_broadcast"):
                    continue

                reactions = None
                if msg.get("reactions"):
                    reactions = json.dumps(msg["reactions"])

                # Extract blocks/attachments for rich content
                blocks = None
                rich_content = {}
                if msg.get("blocks"):
                    rich_content["blocks"] = msg["blocks"]
                if msg.get("attachments"):
                    rich_content["attachments"] = msg["attachments"]
                if rich_content:
                    blocks = json.dumps(rich_content)

                # Extract file attachments
                files = []
                for f in msg.get("files", []):
                    if f.get("url_private"):
                        files.append(SlackFile(
                            id=f["id"],
                            name=f.get("name", f["id"]),
                            mimetype=f.get("mimetype", "application/octet-stream"),
                            url_private=f["url_private"],
                            size=f.get("size", 0),
                            message_ts=msg["ts"],
                            channel_id=channel_id,
                        ))

                all_messages.append(SlackMessage(
                    ts=msg["ts"],
                    user_id=msg.get("user"),
                    username=msg.get("username"),
                    real_name=None,
                    text=msg.get("text", ""),
                    thread_ts=msg.get("thread_ts"),
                    reactions=reactions,
                    channel_id=channel_id,
                    reply_count=msg.get("reply_count", 0),
                    files=files,
                    blocks=blocks,
                ))

            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        return all_messages

    def get_all_channel_messages(
        self,
        channel_id: str,
        oldest: Optional[str] = None,
        limit: Optional[int] = None,
        include_threads: bool = True,
    ) -> list[SlackMessage]:
        """
        Get all messages from a channel since `oldest` timestamp.

        If limit is specified, stop after that many messages.
        If include_threads is True, also fetch replies in threads.
        """
        all_messages = []
        latest = None  # Start from newest
        oldest_float = float(oldest) if oldest else None

        while True:
            # Don't pass oldest to API - it changes pagination behavior
            # Instead, we'll filter and stop when we hit old messages
            messages, next_cursor = self.get_channel_history(
                channel_id=channel_id,
                oldest=None,  # Don't use oldest - filter after
                latest=latest,
            )

            # Filter out messages older than our cutoff
            if oldest_float:
                filtered = [m for m in messages if float(m.ts) > oldest_float]
                # If we filtered some out, we've hit our boundary
                if len(filtered) < len(messages):
                    all_messages.extend(filtered)
                    break
                messages = filtered

            all_messages.extend(messages)

            if limit and len(all_messages) >= limit:
                all_messages = all_messages[:limit]
                break

            if not next_cursor:
                break

            latest = next_cursor

        # Fetch thread replies for messages that are thread parents
        # A message is a thread parent if reply_count > 0 OR (thread_ts == ts)
        if include_threads:
            thread_parents = [
                msg for msg in all_messages
                if msg.reply_count > 0 or (msg.thread_ts and msg.thread_ts == msg.ts)
            ]

            seen_ts = {msg.ts for msg in all_messages}

            for parent in thread_parents:
                try:
                    # Use message ts as thread_ts if not set (for messages with replies but no thread_ts)
                    thread_ts = parent.thread_ts if parent.thread_ts else parent.ts
                    replies = self.get_thread_replies(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        oldest=oldest,
                    )
                    # Add only replies we haven't seen (skip the parent)
                    for reply in replies:
                        if reply.ts not in seen_ts:
                            all_messages.append(reply)
                            seen_ts.add(reply.ts)
                except SlackAPIError:
                    # Skip threads that fail to fetch
                    pass

        return all_messages

    def get_users(self) -> list[SlackUser]:
        """Get all users in the workspace."""
        users = []
        cursor = None

        while True:
            data = {"limit": 200}
            if cursor:
                data["cursor"] = cursor

            result = self._post("users.list", data)

            for user in result.get("members", []):
                if user.get("deleted"):
                    continue
                users.append(SlackUser(
                    id=user["id"],
                    username=user.get("name", ""),
                    real_name=user.get("real_name") or user.get("profile", {}).get("real_name"),
                ))

            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        return users

    def get_user_info(self, user_id: str) -> Optional[SlackUser]:
        """Get info for a specific user."""
        try:
            result = self._post("users.info", {"user": user_id})
            user = result.get("user", {})
            return SlackUser(
                id=user["id"],
                username=user.get("name", ""),
                real_name=user.get("real_name") or user.get("profile", {}).get("real_name"),
            )
        except SlackAPIError:
            return None

    def list_dms(self) -> list[SlackChannel]:
        """List all direct message conversations."""
        dms = []
        cursor = None

        while True:
            data = {
                "types": "im,mpim",
                "limit": 200,
            }
            if cursor:
                data["cursor"] = cursor

            result = self._post("conversations.list", data)

            for ch in result.get("channels", []):
                is_im = ch.get("is_im", False)
                is_mpim = ch.get("is_mpim", False)

                # For regular DMs, get the other user
                user_id = ch.get("user") if is_im else None

                # Generate a readable name
                if is_im:
                    name = f"dm-{user_id}" if user_id else ch["id"]
                else:
                    name = ch.get("name", ch["id"])

                dms.append(SlackChannel(
                    id=ch["id"],
                    name=name,
                    topic=None,
                    purpose=None,
                    member_count=2 if is_im else ch.get("num_members"),
                    is_im=is_im,
                    is_mpim=is_mpim,
                    user_id=user_id,
                ))

            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        return dms

    def download_file(self, file: SlackFile, dest_dir: Path) -> Optional[Path]:
        """
        Download a file attachment to the destination directory.
        Returns the path to the downloaded file, or None if download failed.
        """
        # Create subdirectory structure: {channel_id}/{message_ts}/
        file_dir = dest_dir / file.channel_id / file.message_ts.replace(".", "_")
        file_dir.mkdir(parents=True, exist_ok=True)

        # Sanitize filename
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in file.name)
        dest_path = file_dir / safe_name

        # Skip if already downloaded
        if dest_path.exists() and dest_path.stat().st_size == file.size:
            return dest_path

        try:
            with self._client.stream("GET", file.url_private) as response:
                response.raise_for_status()
                with open(dest_path, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=8192):
                        f.write(chunk)
            return dest_path
        except Exception:
            return None

    def download_files_from_messages(
        self,
        messages: list[SlackMessage],
        dest_dir: Path,
        image_only: bool = True,
    ) -> list[tuple[SlackFile, Path]]:
        """
        Download file attachments from messages.
        Returns list of (file, path) tuples for successfully downloaded files.
        """
        downloaded = []

        for msg in messages:
            for file in msg.files:
                # Filter to images only if requested
                if image_only and not file.mimetype.startswith("image/"):
                    continue

                path = self.download_file(file, dest_dir)
                if path:
                    downloaded.append((file, path))

        return downloaded


class SlackAPIError(Exception):
    """Slack API error."""
    pass
