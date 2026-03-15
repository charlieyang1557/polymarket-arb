# Strategic Compact Command

Before compacting, create a state preservation file:

1. Write current session state to .claude/sessions/pre-compact-{timestamp}.md:
   - Current task and progress
   - Key decisions made this session
   - Open questions and blockers
   - Files modified (git diff --stat)
   - Test status (last pytest result)

2. If working on the MM bot:
   - Current bot status (running? PID? which markets?)
   - Latest P&L from log
   - Any pending code changes not yet committed

3. After saving state, perform /compact

4. After compact completes, read the state file back and
   print a 3-line summary of where we are
