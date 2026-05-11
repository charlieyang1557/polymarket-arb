# Session handoff prompts

Each file is a self-contained prompt for the next Claude Code session
to pick up the project. Read the most recent one first.

| Date | File | Phase |
|---|---|---|
| 2026-05-10 | [handoff_2026-05-10_path_c_execution.md](handoff_2026-05-10_path_c_execution.md) | Original 6-step Path C plan (now completed) |
| 2026-05-11 | [handoff_2026-05-11_validation_and_diagnosis.md](handoff_2026-05-11_validation_and_diagnosis.md) | Path C done; paper-vs-live gap discovered; counterfactual on live; next: per-prefix + round-trip sim + WebSocket collector |

Newest at the bottom.

## Convention

- Each handoff is a single markdown file named
  `handoff_YYYY-MM-DD_<topic>.md`.
- Format: brief framing → "read these first" → "current state of
  repo" → numbered plan → constraints → success criteria → how to
  start.
- Reference relative paths from the worktree root so links work
  when read from the next session's cwd.
- Don't modify previous handoffs — they're historical.
