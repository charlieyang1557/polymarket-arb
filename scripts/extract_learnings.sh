#!/bin/bash
# Extract learnings from session and append to CLAUDE.md
CLAUDE_MD="$HOME/polymarket-arb/CLAUDE.md"
LEARNED_DIR="$HOME/polymarket-arb/.claude/learned"
mkdir -p "$LEARNED_DIR"

DATE=$(date +%Y-%m-%d)
SESSION_FILE="$LEARNED_DIR/session-$DATE.md"

# Create session learnings file
cat >> "$SESSION_FILE" << TEMPLATE

## Session Learnings - $(date)
### Bugs Found
<!-- Auto-populated: any new bugs discovered this session -->

### What Worked
<!-- Approaches that were verified to work -->

### What Failed
<!-- Approaches attempted that did not work -->

### Rules Added/Modified
<!-- Any new rules or CLAUDE.md updates -->

### Open Questions
<!-- Unresolved issues for next session -->
TEMPLATE

echo "[Stop Hook] Session learnings template created at $SESSION_FILE"
echo "[Stop Hook] Review and fill in before next session"
