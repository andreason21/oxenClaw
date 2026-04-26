---
name: healthcheck
description: "Probe gateway internals: channels loaded, isolation backends, cron jobs, session-store size, memory store size."
homepage: https://github.com/oxenclaw
openclaw:
  emoji: "🩺"
---

# healthcheck

One-shot status report. Aggregates per-subsystem checks into a single
text block the LLM (or a dashboard) can read at a glance. Each check is
optional — pass `None` for subsystems the agent doesn't have.
