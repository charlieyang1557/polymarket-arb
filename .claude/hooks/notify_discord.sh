#!/bin/bash
# Sends Discord notification when Claude Code triggers a Notification hook.
# Requires DISCORD_WEBHOOK_URL set in environment (e.g. via .env).

WEBHOOK_URL="${DISCORD_WEBHOOK_URL}"

if [ -z "$WEBHOOK_URL" ]; then
  exit 0
fi

# Claude Code passes the notification message via $CLAUDE_NOTIFICATION
MESSAGE="${CLAUDE_NOTIFICATION:-Claude Code needs attention}"

# Truncate to Discord's 2000-char limit
MESSAGE="${MESSAGE:0:1900}"

curl -s -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d "{\"content\": \"$(echo "$MESSAGE" | sed 's/"/\\"/g' | sed ':a;N;$!ba;s/\n/\\n/g')\"}" \
  > /dev/null 2>&1
