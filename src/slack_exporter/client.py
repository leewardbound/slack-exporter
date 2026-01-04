"""Slack API client using xoxc/xoxd user session tokens."""

import json
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
    files: list[SlackFile] = field(default_factory=list)


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

    def __init__(self, xoxc_token: str, xoxd_token: str, workspace: str = ""):
        """
        Initialize client with user session tokens.

        Args:
            xoxc_token: User client token (xoxc-...)
            xoxd_token: User session cookie (xoxd-...)
            workspace: Optional workspace subdomain (e.g., "daylightxyz")
        """
        self.xoxc_token = xoxc_token
        self.xoxd_token = xoxd_token
        self.workspace = workspace

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

    def _post(self, method: str, data: dict) -> dict:
        """Make a POST request to Slack API."""
        # Include token in form data (not as Authorization header)
        data = {**data, "token": self.xoxc_token}

        response = self._client.post(
            f"{self.base_url}/{method}",
            data=data,
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            error = result.get("error", "Unknown error")
            raise SlackAPIError(f"Slack API error: {error}")
        return result

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
            if msg.get("subtype") and msg.get("subtype") not in ("bot_message", "file_share"):
                continue

            reactions = None
            if msg.get("reactions"):
                reactions = json.dumps(msg["reactions"])

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
                files=files,
            ))

        # Check for pagination
        has_more = result.get("has_more", False)
        next_cursor = None
        if has_more and messages:
            # Use the oldest message timestamp for pagination
            next_cursor = messages[-1].ts

        return messages, next_cursor

    def get_all_channel_messages(
        self,
        channel_id: str,
        oldest: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[SlackMessage]:
        """
        Get all messages from a channel since `oldest` timestamp.

        If limit is specified, stop after that many messages.
        """
        all_messages = []
        latest = None  # Start from newest

        while True:
            messages, next_cursor = self.get_channel_history(
                channel_id=channel_id,
                oldest=oldest,
                latest=latest,
            )

            all_messages.extend(messages)

            if limit and len(all_messages) >= limit:
                all_messages = all_messages[:limit]
                break

            if not next_cursor:
                break

            latest = next_cursor

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
            response = self._client.get(file.url_private)
            response.raise_for_status()

            dest_path.write_bytes(response.content)
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
