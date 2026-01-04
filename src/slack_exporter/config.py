"""Configuration and environment loading."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WorkspaceConfig:
    """Configuration for a Slack workspace."""
    name: str
    subdomain: str
    xoxc_token: str
    xoxd_token: str
    channels: list[str] = field(default_factory=list)


def get_project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent.parent.parent


def load_env_secrets() -> dict[str, str]:
    """Load environment variables from .env.secrets file."""
    env_path = get_project_root() / ".env.secrets"
    env_vars = {}

    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                env_vars[key.strip()] = value.strip()

    return env_vars


def load_channels_config() -> dict[str, list[str]]:
    """
    Load channels from channels.txt file.
    Returns dict mapping workspace name to list of channel names.
    """
    channels_path = get_project_root() / "channels.txt"
    workspace_channels: dict[str, list[str]] = {}

    if channels_path.exists():
        for line in channels_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "/" in line:
                workspace, channel = line.split("/", 1)
                workspace = workspace.strip().lower()
                channel = channel.strip().lstrip("#")
                if workspace not in workspace_channels:
                    workspace_channels[workspace] = []
                if channel not in workspace_channels[workspace]:
                    workspace_channels[workspace].append(channel)

    return workspace_channels


def get_workspaces() -> list[WorkspaceConfig]:
    """Get configured workspaces with their tokens and channels."""
    env = load_env_secrets()
    channels_config = load_channels_config()

    workspaces = []

    # Find all configured workspaces by looking for *_XOXC_TOKEN patterns
    workspace_names = set()
    for key in env:
        if key.endswith("_XOXC_TOKEN"):
            ws_name = key.replace("_XOXC_TOKEN", "").lower()
            workspace_names.add(ws_name)

    for ws_name in sorted(workspace_names):
        ws_upper = ws_name.upper()
        xoxc = env.get(f"{ws_upper}_XOXC_TOKEN", "")
        xoxd = env.get(f"{ws_upper}_XOXD_TOKEN", "")
        subdomain = env.get(f"{ws_upper}_SUBDOMAIN", ws_name)

        if not xoxc or not xoxd:
            continue

        channels = channels_config.get(ws_name, [])

        workspaces.append(WorkspaceConfig(
            name=ws_name,
            subdomain=subdomain,
            xoxc_token=xoxc,
            xoxd_token=xoxd,
            channels=channels,
        ))

    return workspaces


def get_db_path() -> Path:
    """Get path to SQLite database."""
    return get_project_root() / "data" / "slack.db"


def get_attachments_dir() -> Path:
    """Get path to attachments directory."""
    return get_project_root() / "attachments"
