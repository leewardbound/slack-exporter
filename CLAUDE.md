# Slack Exporter

A Python tool to export Slack messages to a local SQLite database using user session tokens.

## Overview

This tool uses your Slack user session tokens (xoxc/xoxd) to pull messages from specified channels and DMs, download image attachments, and save everything locally. It's designed for personal archiving and runs independently of any AI/MCP tooling.

## Authentication

Uses browser session tokens (not bot tokens):
- `{WORKSPACE}_XOXC_TOKEN` - Client token (passed in form data as `token` param)
- `{WORKSPACE}_XOXD_TOKEN` - Session cookie (URL-encoded, set as `d=` cookie header)
- `{WORKSPACE}_SUBDOMAIN` - Workspace subdomain (e.g., "myworkspace")

Tokens are stored in `.env.secrets` (gitignored).

## Configuration

### .env.secrets
```
MYCOMPANY_XOXC_TOKEN=xoxc-...
MYCOMPANY_XOXD_TOKEN=xoxd-...
MYCOMPANY_SUBDOMAIN=mycompany
```

### channels.txt
```
# Format: workspace/channel (one per line)
# Use workspace/* to subscribe to all channels in a workspace
mycompany/general
mycompany/engineering
otherworkspace/*
```

## Project Structure

```
slack-exporter/
├── src/slack_exporter/
│   ├── __init__.py
│   ├── client.py       # Slack API client (xoxc/xoxd auth)
│   ├── config.py       # Loads from .env.secrets and channels.txt
│   ├── sync.py         # Shared incremental sync logic
│   └── storage.py      # SQLite storage
├── scripts/
│   ├── sync.py         # Full sync script
│   ├── incremental.py  # Cron-friendly incremental sync
│   ├── daemon.py       # RTM WebSocket daemon
│   ├── recent.py       # Show recent conversations
│   ├── active_topics.py # Show active threads
│   └── thread.py       # Show full thread context
├── data/slack.db       # SQLite database (gitignored)
├── attachments/        # Downloaded images (gitignored)
├── channels.txt        # Channel config (gitignored, see channels.txt.example)
├── .env.secrets        # Tokens (gitignored)
└── CLAUDE.md
```

## Database Schema

- **workspaces** - Slack workspaces with last sync time
- **channels** - Channel/DM metadata (id, name, is_dm)
- **users** - User metadata (id, username, real_name)
- **messages** - Message content with timestamps, threads, reactions
- **attachments** - Downloaded file metadata with local paths

## Usage

```bash
# Full sync (last 90 days by default, includes DMs and attachments)
uv run python scripts/sync.py

# Customize sync
uv run python scripts/sync.py --days 30           # Last 30 days
uv run python scripts/sync.py --no-dms            # Skip DMs
uv run python scripts/sync.py --no-attachments    # Skip image downloads
uv run python scripts/sync.py --workspace mycompany  # Specific workspace

# Incremental sync (for cron) - catches up after any downtime
uv run python scripts/incremental.py
```

## Crontab Example

```
# Sync every 15 minutes (uses last-seen timestamp per channel, catches up after downtime)
*/15 * * * * cd /path/to/slack-exporter && uv run python scripts/incremental.py >> /tmp/slack-sync.log 2>&1
```

## Claude Code Skill

This project includes a Claude Code skill for querying the Slack archive. The skill teaches Claude how to query the local SQLite database for message history, threads, and active topics.

To install the skill (symlinks into `~/.claude/skills/`):

```bash
make install-skill
```

After installing, Claude Code will automatically use the `slack-history` skill when you ask about Slack messages, conversations, or channel history.

## Development

```bash
# Install dependencies
uv sync

# Test sync
uv run python scripts/sync.py --days 7 --no-dms
```
