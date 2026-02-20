---
name: slack-history
description: Query local Slack message archive from SQLite database. Use when user asks about Slack messages, conversations, history, DMs, channels, or anything about "what did X say" or "find that message".
---

# Slack History

Query the local Slack archive stored in SQLite. Messages sync via cron or daemon.

## Database Location

The database is at `data/slack.db` relative to the slack-exporter project root. Find it with:

```bash
# If SLACK_EXPORTER_DIR is set
$SLACK_EXPORTER_DIR/data/slack.db

# Otherwise, find it
find ~ -name slack.db -path "*/slack-exporter/data/*" 2>/dev/null | head -1
```

## Schema

```sql
-- Workspaces
workspaces(name TEXT PRIMARY KEY, last_sync TEXT)

-- Channels (including DMs)
channels(id TEXT, workspace TEXT, name TEXT, topic TEXT, purpose TEXT,
         member_count INTEGER, is_dm INTEGER DEFAULT 0)

-- Users
users(id TEXT, workspace TEXT, username TEXT, real_name TEXT)

-- Messages
messages(id TEXT, workspace TEXT, channel_id TEXT, user_id TEXT,
         text TEXT, timestamp TEXT, thread_ts TEXT, reactions TEXT,
         latest_reply TEXT,  -- timestamp of most recent reply (for thread parents)
         blocks TEXT)  -- JSON of rich content (blocks/attachments)
-- Indexes: timestamp, channel_id+workspace, thread_ts
-- Note: thread_ts links replies to parent. If thread_ts = id, it's a parent message.

-- Attachments (downloaded images)
attachments(id TEXT, workspace TEXT, channel_id TEXT, message_ts TEXT,
            name TEXT, mimetype TEXT, size INTEGER, local_path TEXT)
```

## Thread Context (IMPORTANT)

**Always retrieve full thread context when viewing messages.** Messages in Slack are often part of threaded conversations. When you see a message with a `thread_ts` value, retrieve the entire thread to understand context.

### Quick thread lookup (recommended)
```bash
uv run python scripts/thread.py 1736000000.123456
uv run python scripts/thread.py 1736000000.123456 -w myworkspace
uv run python scripts/thread.py 1736000000.123456 --json
```

### Get full thread for a message
```sql
SELECT m.timestamp, u.real_name, m.text, m.id
FROM messages m
LEFT JOIN users u ON m.user_id = u.id AND m.workspace = u.workspace
WHERE m.thread_ts = '1736000000.123456'
ORDER BY m.timestamp ASC;
```

### Get thread context for a specific message
```sql
-- Step 1: Find message and its thread_ts
SELECT id, thread_ts, text FROM messages WHERE id = '1736000000.789012';

-- Step 2: If thread_ts is not null, get the whole thread
SELECT m.timestamp, u.real_name, m.text,
       CASE WHEN m.id = m.thread_ts THEN '[PARENT]' ELSE '' END as role
FROM messages m
LEFT JOIN users u ON m.user_id = u.id AND m.workspace = u.workspace
WHERE m.thread_ts = '1736000000.123456'
ORDER BY m.timestamp ASC;
```

### Find messages with active threads (recent replies)
```sql
SELECT m.id, m.timestamp, u.real_name, m.text, m.latest_reply,
       (SELECT COUNT(*) FROM messages m2 WHERE m2.thread_ts = m.id) as reply_count
FROM messages m
LEFT JOIN users u ON m.user_id = u.id AND m.workspace = u.workspace
WHERE m.thread_ts = m.id
  AND m.latest_reply IS NOT NULL
ORDER BY m.latest_reply DESC
LIMIT 10;
```

## Common Queries

### List workspaces
```sql
SELECT name, last_sync FROM workspaces;
```

### Recent messages from a channel
```sql
SELECT m.timestamp, u.real_name, m.text
FROM messages m
LEFT JOIN users u ON m.user_id = u.id AND m.workspace = u.workspace
WHERE m.channel_id = (SELECT id FROM channels WHERE name = 'general' LIMIT 1)
ORDER BY m.timestamp DESC
LIMIT 20;
```

### Search messages by text
```sql
SELECT m.timestamp, u.real_name, c.name as channel, m.text
FROM messages m
LEFT JOIN users u ON m.user_id = u.id AND m.workspace = u.workspace
LEFT JOIN channels c ON m.channel_id = c.id AND m.workspace = c.workspace
WHERE m.text LIKE '%search term%'
ORDER BY m.timestamp DESC
LIMIT 20;
```

### Messages from a specific user
```sql
SELECT m.timestamp, c.name as channel, m.text
FROM messages m
JOIN users u ON m.user_id = u.id AND m.workspace = u.workspace
JOIN channels c ON m.channel_id = c.id AND m.workspace = c.workspace
WHERE u.username = 'john' OR u.real_name LIKE '%John%'
ORDER BY m.timestamp DESC
LIMIT 20;
```

### DMs
```sql
SELECT m.timestamp, u.real_name, m.text
FROM messages m
LEFT JOIN users u ON m.user_id = u.id AND m.workspace = u.workspace
JOIN channels c ON m.channel_id = c.id AND m.workspace = c.workspace
WHERE c.is_dm = 1
ORDER BY m.timestamp DESC
LIMIT 50;
```

### List channels
```sql
SELECT name, member_count, is_dm FROM channels
ORDER BY member_count DESC;
```

## Active Topics (Recommended for Context)

Shows threads with recent activity, including first 5 messages (context) and last 20 messages (recent).

```bash
uv run python scripts/active_topics.py
uv run python scripts/active_topics.py -w myworkspace
uv run python scripts/active_topics.py --hours 48
uv run python scripts/active_topics.py --json
```

## Quick Overview of Active Conversations

For a simpler view (just recent messages per channel, not thread-grouped):

```bash
uv run python scripts/recent.py
uv run python scripts/recent.py -w myworkspace
uv run python scripts/recent.py --hours 48 --messages 10
```

## Triggering a Refresh

```bash
# Quick incremental sync
uv run python scripts/incremental.py

# Full 90-day resync
uv run python scripts/sync.py

# Check status
make status
```

## Notes

- Timestamps are ISO 8601 format (sortable as strings)
- `reactions` field is JSON when present
- Images are downloaded to `attachments/` dir with paths in `local_path`
- DMs are stored as channels with `is_dm = 1`
