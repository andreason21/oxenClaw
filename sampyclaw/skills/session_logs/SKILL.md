---
name: session-logs
description: "Inspect the agent's own session transcripts: list recent sessions, view a session, or grep across all sessions for a phrase."
homepage: https://github.com/sampyclaw
openclaw:
  emoji: "📜"
---

# session-logs

A meta-tool: lets the agent introspect its own conversational history.
Backed by `SessionManager` (no extra storage).

Actions:
- `list` — recent sessions (most-recently-updated first).
- `view` — show one session's last N turns.
- `grep` — find a substring across all sessions; returns matching turns
  with session id + index for follow-up `view`.
