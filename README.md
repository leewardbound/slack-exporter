# Slack Exporter

A Python tool to export Slack messages to a local SQLite database using user session tokens. Designed for personal archiving - pull messages from channels and DMs, download image attachments, and query everything locally.

## Features

- Sync messages from any channels/DMs you have access to
- Subscribe to individual channels or entire workspaces with `workspace/*`
- Download image attachments locally
- Incremental sync (only fetches new messages)
- Thread-aware: captures full thread context including replies
- RTM WebSocket daemon for real-time sync
- Cron-friendly incremental script with catch-up after downtime
- SQLite database for easy querying
- Claude Code skill for AI-assisted Slack history search

## Quick Start

```bash
# Install dependencies
uv sync

# Copy example config and edit with your tokens
cp channels.txt.example channels.txt

# Create .env.secrets with your workspace tokens (see Authentication below)

# Run first sync (last 90 days)
uv run python scripts/sync.py
```

## Authentication

This tool uses browser session tokens (not bot tokens). You'll need to extract `xoxc` and `xoxd` tokens from your browser session.

### Getting Your Tokens

1. Open Slack in your browser
2. Open Developer Tools (F12) > Network tab
3. Send a message or perform any action in Slack
4. Find a request to `api/` and look at the form data for the `token` field (`xoxc-...`)
5. Look at the request cookies for the `d` cookie (`xoxd-...`)

### .env.secrets

Create a `.env.secrets` file (gitignored) with tokens for each workspace:

```
MYCOMPANY_XOXC_TOKEN=xoxc-...
MYCOMPANY_XOXD_TOKEN=xoxd-...
MYCOMPANY_SUBDOMAIN=mycompany

PERSONAL_XOXC_TOKEN=xoxc-...
PERSONAL_XOXD_TOKEN=xoxd-...
PERSONAL_SUBDOMAIN=my-personal-workspace
```

The workspace name prefix (e.g., `MYCOMPANY`) is how you reference it in `channels.txt`.

## Configuration

### channels.txt

Specify which channels to sync (one per line). Use `workspace/*` to subscribe to all channels:

```
# Specific channels
mycompany/general
mycompany/engineering
mycompany/random

# All channels in a workspace
personal/*
```

See `channels.txt.example` for a template.

## Usage

### Full Sync

```bash
# Sync all configured channels (last 90 days)
uv run python scripts/sync.py

# Customize
uv run python scripts/sync.py --days 30           # Last 30 days
uv run python scripts/sync.py --no-dms            # Skip DMs
uv run python scripts/sync.py --no-attachments    # Skip image downloads
uv run python scripts/sync.py --workspace mycompany  # Specific workspace
uv run python scripts/sync.py --channel general   # Specific channel
```

### Incremental Sync (Cron)

```bash
# Syncs only new messages since last run
uv run python scripts/incremental.py
```

Crontab example:
```
*/15 * * * * cd /path/to/slack-exporter && uv run python scripts/incremental.py >> /tmp/slack-sync.log 2>&1
```

### Real-Time Daemon

```bash
# Run via Docker
make up

# Or run locally
uv run python scripts/daemon.py
```

### Querying

```bash
# Recent conversations (last 24h)
uv run python scripts/recent.py

# Active threads with context
uv run python scripts/active_topics.py

# Full thread by timestamp
uv run python scripts/thread.py 1736000000.123456
```

Or query the SQLite database directly:

```bash
sqlite3 data/slack.db "SELECT m.timestamp, u.real_name, m.text FROM messages m LEFT JOIN users u ON m.user_id = u.id AND m.workspace = u.workspace ORDER BY m.timestamp DESC LIMIT 20;"
```

## Database Schema

All data lives in `data/slack.db`:

- **workspaces** - Slack workspaces with last sync time
- **channels** - Channel/DM metadata (id, name, is_dm)
- **users** - User metadata (id, username, real_name)
- **messages** - Message content with timestamps, threads, reactions, rich blocks
- **attachments** - Downloaded file metadata with local paths
- **rate_limits** - API rate limit events for monitoring

## Claude Code Skill

This project includes a [Claude Code](https://claude.com/claude-code) skill that teaches Claude how to query your local Slack archive.

### Installing the Skill

```bash
make install-skill
```

This symlinks `skills/slack-history/` into `~/.claude/skills/` so Claude Code can use it across any project. After installing, you can ask Claude things like:

- "What did the team discuss about the API redesign?"
- "Find messages from Alice about the deployment"
- "Show me active threads from the last 48 hours"

## Makefile Commands

```bash
make up              # Start RTM daemon (Docker)
make down            # Stop daemon
make incremental     # Run incremental sync
make full            # Full 90-day sync
make recent          # Show recent conversations
make topics          # Show active threads
make status          # Show sync status
make rate-limits     # Show rate limit stats
make install-skill   # Install Claude Code skill
```

## License

MIT
