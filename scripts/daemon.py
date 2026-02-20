#!/usr/bin/env python3
"""
Real-time Slack message sync daemon using RTM WebSocket.

Connects to Slack's RTM API and stores messages as they arrive.
On every connect/reconnect, runs an incremental sync via REST API
to catch up on any messages missed while offline.

Also runs a periodic background sync every 30 minutes as a safety net.

Usage:
    # Run directly
    uv run python scripts/daemon.py

    # Or via Docker Compose
    docker compose up -d
"""

import asyncio
import json
import signal
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import websockets

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from slack_exporter.config import get_workspaces, get_db_path, get_attachments_dir, WorkspaceConfig
from slack_exporter.storage import Storage, User, Message, Attachment
from slack_exporter.sync import sync_workspace_incremental

# How often to run background incremental sync (seconds)
PERIODIC_SYNC_INTERVAL = 1800  # 30 minutes


class SlackRTMClient:
    """RTM WebSocket client for a single workspace."""

    def __init__(
        self,
        config: WorkspaceConfig,
        storage: Storage,
        attachments_dir: Path,
        log_fn,
    ):
        self.config = config
        self.storage = storage
        self.attachments_dir = attachments_dir
        self.log = log_fn
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.running = False
        self._http_client: Optional[httpx.AsyncClient] = None

    @property
    def name(self) -> str:
        return self.config.name

    def _get_headers(self) -> dict:
        """Get headers for Slack API requests."""
        xoxd_encoded = urllib.parse.quote(self.config.xoxd_token, safe='')
        return {
            "Cookie": f"d={xoxd_encoded}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    async def _get_ws_url(self) -> str:
        """Get WebSocket URL via rtm.connect."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://{self.config.subdomain}.slack.com/api/rtm.connect",
                data={"token": self.config.xoxc_token},
                headers=self._get_headers(),
            )
            result = response.json()
            if not result.get("ok"):
                raise Exception(f"rtm.connect failed: {result.get('error')}")
            return result["url"]

    def parse_slack_ts(self, ts: str) -> datetime:
        """Parse Slack timestamp to datetime."""
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)

    async def catchup(self):
        """Catch up on missed messages via REST API incremental sync."""
        self.log(f"{self.name}: catching up on missed messages...")
        try:
            msg_count, att_count = await asyncio.to_thread(
                sync_workspace_incremental,
                self.config,
                self.storage,
                self.attachments_dir,
                self.log,
            )
            self.log(f"{self.name}: catchup complete: {msg_count} msgs, {att_count} atts")
        except Exception as e:
            self.log(f"{self.name}: catchup error: {e}")

    async def handle_message(self, event: dict):
        """Handle incoming message event."""
        subtype = event.get("subtype")
        channel_id = event.get("channel")

        # Handle message edits - extract the updated message
        if subtype == "message_changed":
            msg_data = event.get("message", {})
            ts = msg_data.get("ts")
            user_id = msg_data.get("user")
            text = msg_data.get("text", "")
            thread_ts = msg_data.get("thread_ts")
            is_edit = True
        elif subtype == "message_deleted":
            # Could track deletions in the future, skip for now
            return
        elif subtype and subtype not in ("bot_message", "file_share", "thread_broadcast"):
            return
        else:
            ts = event.get("ts")
            user_id = event.get("user")
            text = event.get("text", "")
            thread_ts = event.get("thread_ts")
            is_edit = False
            msg_data = event

        if not channel_id or not ts:
            return

        # Build reactions JSON if present
        reactions = None
        if msg_data.get("reactions"):
            reactions = json.dumps(msg_data["reactions"])

        # Build blocks JSON for rich content
        blocks = None
        rich_content = {}
        if msg_data.get("blocks"):
            rich_content["blocks"] = msg_data["blocks"]
        if msg_data.get("attachments"):
            rich_content["attachments"] = msg_data["attachments"]
        if rich_content:
            blocks = json.dumps(rich_content)

        # Create message record
        message = Message(
            id=ts,
            workspace=self.name,
            channel_id=channel_id,
            user_id=user_id,
            text=text,
            timestamp=self.parse_slack_ts(ts),
            thread_ts=thread_ts,
            reactions=reactions,
            latest_reply=msg_data.get("latest_reply"),
            blocks=blocks,
        )

        self.storage.upsert_messages_batch([message])

        # Fetch user info if needed
        if user_id:
            await self.ensure_user(user_id)

        # Handle file attachments
        if msg_data.get("files"):
            await self.handle_files(channel_id, ts, msg_data["files"])

        action = "edit" if is_edit else "msg"
        self.log(f"{self.name}: {action} in {channel_id[:8]}... from {user_id or 'bot'}")

    async def handle_files(self, channel_id: str, message_ts: str, files: list):
        """Download and store file attachments."""
        if not self._http_client:
            self._http_client = httpx.AsyncClient(headers=self._get_headers())

        for f in files:
            url = f.get("url_private")
            if not url:
                continue

            file_id = f["id"]
            name = f.get("name", file_id)
            mimetype = f.get("mimetype", "application/octet-stream")
            size = f.get("size", 0)

            # Create destination path
            file_dir = self.attachments_dir / channel_id / message_ts.replace(".", "_")
            file_dir.mkdir(parents=True, exist_ok=True)
            safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
            dest_path = file_dir / safe_name

            # Skip if already downloaded
            if dest_path.exists() and dest_path.stat().st_size == size:
                continue

            try:
                response = await self._http_client.get(url, follow_redirects=True)
                if response.status_code == 200:
                    dest_path.write_bytes(response.content)

                    attachment = Attachment(
                        id=file_id,
                        workspace=self.name,
                        channel_id=channel_id,
                        message_ts=message_ts,
                        name=name,
                        mimetype=mimetype,
                        size=size,
                        local_path=str(dest_path),
                    )
                    self.storage.upsert_attachments_batch([attachment])
                    self.log(f"{self.name}: downloaded {name}")
            except Exception as e:
                self.log(f"{self.name}: failed to download {name}: {e}")

    async def ensure_user(self, user_id: str):
        """Ensure user info is stored."""
        if not self._http_client:
            self._http_client = httpx.AsyncClient(headers=self._get_headers())

        try:
            response = await self._http_client.post(
                f"https://{self.config.subdomain}.slack.com/api/users.info",
                data={"token": self.config.xoxc_token, "user": user_id},
            )
            result = response.json()
            if result.get("ok"):
                user_data = result["user"]
                user = User(
                    id=user_id,
                    workspace=self.name,
                    username=user_data.get("name", ""),
                    real_name=user_data.get("real_name") or user_data.get("profile", {}).get("real_name"),
                )
                self.storage.upsert_user(user)
        except Exception:
            pass

    async def handle_event(self, event: dict):
        """Route events to handlers."""
        event_type = event.get("type")

        if event_type == "hello":
            self.log(f"{self.name}: connected to RTM")
        elif event_type == "message":
            await self.handle_message(event)
        elif event_type == "reaction_added":
            self.log(f"{self.name}: reaction added")
        elif event_type == "reaction_removed":
            self.log(f"{self.name}: reaction removed")
        elif event_type == "goodbye":
            self.log(f"{self.name}: server requested disconnect")
        elif event_type == "error":
            self.log(f"{self.name}: error: {event}")

    async def connect(self):
        """Connect to RTM WebSocket and process events."""
        self.running = True
        retry_delay = 1

        while self.running:
            try:
                # Catch up on any messages missed while offline
                await self.catchup()

                ws_url = await self._get_ws_url()
                self.log(f"{self.name}: connecting to RTM...")

                async with websockets.connect(ws_url, additional_headers=self._get_headers()) as ws:
                    self.ws = ws
                    retry_delay = 1  # Reset on successful connect

                    async for raw_message in ws:
                        if not self.running:
                            break
                        try:
                            event = json.loads(raw_message)
                            await self.handle_event(event)
                        except json.JSONDecodeError:
                            self.log(f"{self.name}: invalid JSON: {raw_message[:100]}")

            except websockets.ConnectionClosed as e:
                self.log(f"{self.name}: connection closed: {e}")
            except Exception as e:
                self.log(f"{self.name}: error: {e}")

            if self.running:
                self.log(f"{self.name}: reconnecting in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

    async def close(self):
        """Close the connection."""
        self.running = False
        if self.ws:
            await self.ws.close()
        if self._http_client:
            await self._http_client.aclose()


class SlackDaemon:
    """Real-time Slack sync daemon."""

    def __init__(self, storage: Storage, attachments_dir: Path):
        self.storage = storage
        self.attachments_dir = attachments_dir
        self.clients: list[SlackRTMClient] = []
        self.running = False

    def log(self, msg: str):
        """Log with timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        print(f"[{now}] {msg}", flush=True)

    async def _periodic_sync(self):
        """Run incremental sync periodically to catch anything RTM might miss."""
        while self.running:
            await asyncio.sleep(PERIODIC_SYNC_INTERVAL)
            if not self.running:
                break
            self.log("Starting periodic background sync...")
            for client in self.clients:
                try:
                    await client.catchup()
                except Exception as e:
                    self.log(f"Periodic sync error for {client.name}: {e}")
            self.log("Periodic background sync complete")

    async def start(self):
        """Start the daemon."""
        self.log("Starting Slack RTM daemon...")
        self.running = True

        workspaces = get_workspaces()
        if not workspaces:
            self.log("No workspaces configured!")
            return

        # Create clients for each workspace
        for ws_config in workspaces:
            self.storage.upsert_workspace(ws_config.name)
            client = SlackRTMClient(
                config=ws_config,
                storage=self.storage,
                attachments_dir=self.attachments_dir,
                log_fn=self.log,
            )
            self.clients.append(client)

        self.log(f"Connecting to {len(self.clients)} workspace(s)...")

        # Run all RTM clients + periodic sync concurrently
        tasks = [asyncio.create_task(client.connect()) for client in self.clients]
        tasks.append(asyncio.create_task(self._periodic_sync()))

        # Wait for shutdown signal
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        """Stop the daemon gracefully."""
        self.log("Stopping daemon...")
        self.running = False

        for client in self.clients:
            await client.close()
            self.log(f"{client.name}: closed")

        self.log("Daemon stopped")


async def main():
    db_path = get_db_path()
    attachments_dir = get_attachments_dir()
    storage = Storage(db_path)

    daemon = SlackDaemon(storage, attachments_dir)

    # Handle signals for graceful shutdown
    loop = asyncio.get_event_loop()

    def signal_handler():
        asyncio.create_task(daemon.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    await daemon.start()


if __name__ == "__main__":
    asyncio.run(main())
