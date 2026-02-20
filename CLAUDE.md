# Slack Exporter

See README.md for full documentation.

## Key Paths

- Database: `data/slack.db` (gitignored)
- Tokens: `.env.secrets` (gitignored)
- Channel config: `channels.txt` (gitignored, see `channels.txt.example`)
- Attachments: `attachments/` (gitignored)

## Architecture

- `src/slack_exporter/client.py` - Slack API client (xoxc/xoxd auth, rate limiting)
- `src/slack_exporter/config.py` - Loads `.env.secrets` and `channels.txt`, supports `workspace/*` wildcards
- `src/slack_exporter/storage.py` - SQLite schema and CRUD operations
- `src/slack_exporter/sync.py` - Shared incremental sync logic (used by cron script and daemon)
- `scripts/sync.py` - Full sync script (CLI)
- `scripts/incremental.py` - Cron-friendly incremental sync
- `scripts/daemon.py` - RTM WebSocket daemon for real-time sync
- `scripts/recent.py` - Show recent conversations
- `scripts/active_topics.py` - Show active threads with context
- `scripts/thread.py` - Show full thread by timestamp

## Token Format

Env vars in `.env.secrets` follow `{WORKSPACE}_XOXC_TOKEN` / `{WORKSPACE}_XOXD_TOKEN` / `{WORKSPACE}_SUBDOMAIN` pattern. The workspace prefix (uppercased) maps to the lowercase name used in `channels.txt`.
