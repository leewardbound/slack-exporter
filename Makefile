.PHONY: daemon up down restart logs-daemon sync incremental full status logs recent rate-limits install-skill

# Default: start daemon
daemon: up

# Start RTM daemon (real-time sync via WebSocket)
up:
	docker compose up -d

# Stop daemon
down:
	docker compose down

# Restart daemon
restart:
	docker compose restart

# Follow daemon logs
logs-daemon:
	docker compose logs -f

# Run daemon locally (without Docker)
daemon-local:
	uv run python scripts/daemon.py

# Manual incremental sync (for backfill after downtime)
incremental:
	uv run python scripts/incremental.py

# Full sync (last 90 days, all channels/DMs/attachments)
full:
	uv run python scripts/sync.py

# Full sync with custom options
full-30:
	uv run python scripts/sync.py --days 30

full-no-dms:
	uv run python scripts/sync.py --no-dms

# Get full thread context for a message
thread:
	@echo "Usage: make thread TS=1736000000.123456"
	@echo "       make thread TS=1736000000.123456 WORKSPACE=mycompany"

thread-%:
	uv run python scripts/thread.py $* $(if $(WORKSPACE),-w $(WORKSPACE))

# Show recent/active conversations (last 24h with latest 5 msgs each)
recent:
	uv run python scripts/recent.py

# Show active topics (threads with recent activity - first 5 + last 20 msgs)
topics:
	uv run python scripts/active_topics.py

topics-48h:
	uv run python scripts/active_topics.py --hours 48

# Show sync status
status:
	@echo "=== Sync Status ==="
	@echo ""
	@echo "Daemon status:"
	@docker compose ps 2>/dev/null || echo "Docker not running"
	@echo ""
	@echo "Messages per workspace:"
	@sqlite3 data/slack.db "SELECT workspace, COUNT(*) as msgs FROM messages GROUP BY workspace;"
	@echo ""
	@echo "Latest messages:"
	@sqlite3 data/slack.db "SELECT workspace, MAX(timestamp) as latest FROM messages GROUP BY workspace;"

# Follow daemon logs (alias)
logs: logs-daemon

# Install Claude Code skill (symlinks to ~/.claude/skills/)
install-skill:
	@mkdir -p ~/.claude/skills
	@ln -sfn $(CURDIR)/skills/slack-history ~/.claude/skills/slack-history
	@echo "Installed slack-history skill -> ~/.claude/skills/slack-history"

# Show rate limit stats (last 24h)
rate-limits:
	@echo "=== Rate Limit Stats (last 24h) ==="
	@echo ""
	@echo "Total rate limits:"
	@sqlite3 data/slack.db "SELECT COUNT(*) FROM rate_limits WHERE timestamp > datetime('now', '-24 hours');" 2>/dev/null || echo "0"
	@echo ""
	@echo "By workspace:"
	@sqlite3 data/slack.db "SELECT workspace, COUNT(*) as cnt FROM rate_limits WHERE timestamp > datetime('now', '-24 hours') GROUP BY workspace ORDER BY cnt DESC;" 2>/dev/null || echo "None"
	@echo ""
	@echo "By method:"
	@sqlite3 data/slack.db "SELECT method, COUNT(*) as cnt FROM rate_limits WHERE timestamp > datetime('now', '-24 hours') GROUP BY method ORDER BY cnt DESC LIMIT 10;" 2>/dev/null || echo "None"
	@echo ""
	@echo "By hour (last 24h):"
	@sqlite3 data/slack.db "SELECT strftime('%Y-%m-%d %H:00', timestamp) as hour, COUNT(*) as cnt FROM rate_limits WHERE timestamp > datetime('now', '-24 hours') GROUP BY hour ORDER BY hour DESC;" 2>/dev/null || echo "None"
