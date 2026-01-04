# Slack Exporter

A Python tool to export Slack messages to a local SQLite database using user session tokens.

## Overview

This tool uses your Slack user session tokens (xoxc/xoxd) to pull messages from specified channels and DMs, download image attachments, and save everything locally. It's designed for personal archiving and runs independently of any AI/MCP tooling.

## Authentication

Uses browser session tokens (not bot tokens):
- `{WORKSPACE}_XOXC_TOKEN` - Client token (passed in form data as `token` param)
- `{WORKSPACE}_XOXD_TOKEN` - Session cookie (URL-encoded, set as `d=` cookie header)
- `{WORKSPACE}_SUBDOMAIN` - Workspace subdomain (e.g., "daylightxyz")

Tokens are stored in `.env.secrets` (gitignored).

## Configuration

### .env.secrets
```
DAYLIGHT_XOXC_TOKEN=xoxc-...
DAYLIGHT_XOXD_TOKEN=xoxd-...
DAYLIGHT_SUBDOMAIN=daylightxyz

BOUNDCORP_XOXC_TOKEN=xoxc-...
BOUNDCORP_XOXD_TOKEN=xoxd-...
BOUNDCORP_SUBDOMAIN=boundcorporation
```

### channels.txt
```
# Format: workspace/channel (one per line)
daylight/backend
daylight/general
daylight/alerts-api
boundcorp/general
```

## Project Structure

```
slack-exporter/
├── src/slack_exporter/
│   ├── __init__.py
│   ├── client.py       # Slack API client (xoxc/xoxd auth)
│   ├── config.py       # Loads from .env.secrets and channels.txt
│   └── storage.py      # SQLite storage
├── scripts/
│   ├── sync.py         # Full sync script
│   └── incremental.py  # Cron-friendly incremental sync (catches up after downtime)
├── data/slack.db       # SQLite database (gitignored)
├── attachments/        # Downloaded images (gitignored)
├── channels.txt        # Channel config
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
uv run python scripts/sync.py --workspace daylight  # Specific workspace

# Incremental sync (for cron) - catches up after any downtime
uv run python scripts/incremental.py
```

## Crontab Example

```
# Sync every 15 minutes (uses last-seen timestamp per channel, catches up after downtime)
*/15 * * * * cd /home/linked/p/leeward/slack-exporter && /home/linked/.config/python/bin/uv run python scripts/incremental.py >> /tmp/slack-sync.log 2>&1
```

## Current Stats

After 90-day sync:
- 1,378 messages
- 178 image attachments
- 76 channels/DMs
- 64 users

## Development

```bash
# Install dependencies
uv sync

# Test sync
uv run python scripts/sync.py --days 7 --no-dms
```
