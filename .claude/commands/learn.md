# Learn Command
When invoked, extract the current session's key learnings:

1. Read the current conversation for:
   - Bugs discovered and their root causes
   - Approaches that worked (with evidence)
   - Approaches that failed (with reason)
   - New rules or patterns established

2. Append findings to CLAUDE.md under "## Lessons Learned"

3. If a new recurring pattern is found, add it to the
   appropriate .claude/rules/ file

4. Format: "YYYY-MM-DD: [category] description"
   Example: "2026-03-14: [bug] FULL_STOP sets all markets
   inactive but only logs the triggering market"
