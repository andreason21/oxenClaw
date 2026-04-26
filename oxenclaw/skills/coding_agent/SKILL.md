---
name: coding-agent
description: "Delegate feature work, PR review, refactors, or iterative coding to a background CLI coding agent (Claude Code, Codex, opencode, or pi)."
homepage: https://github.com/oxenclaw
openclaw:
  emoji: "🧩"
  requires:
    anyBins: ["claude", "codex", "opencode", "pi"]
  install:
    - id: node-claude
      kind: node
      package: "@anthropic-ai/claude-code"
      bins: ["claude"]
      label: "Install Claude Code CLI (npm)"
    - id: node-codex
      kind: node
      package: "@openai/codex"
      bins: ["codex"]
      label: "Install Codex CLI (npm)"
    - id: opencode
      kind: brew
      package: "opencode"
      bins: ["opencode"]
      label: "Install opencode (Homebrew)"
    - id: pi
      kind: pip
      package: "pi-coding-agent"
      bins: ["pi"]
      label: "Install pi (pip)"
  workspace:
    kind: ephemeral
    retain_on_error: true
  env_overrides:
    OXENCLAW_CODING_AGENT: "1"
---

# coding-agent

Delegates focused coding tasks to a sub-process backed by Claude Code,
Codex, opencode, or pi. The host LLM hands off a high-level task and
the chosen CLI runs in an ephemeral workspace, returning a final
summary.

## When to use

- Multi-file feature work where the host LLM would benefit from the
  CLI's repo-aware tooling (test runners, language servers, lint).
- PR review at scale — the CLI walks the diff, the host LLM consumes
  the bullet-pointed result.
- Long iterative refactors where a fresh process keeps context tidy.

## Selection

The skill picks the first available CLI in this preference order:

1. `claude` (Claude Code, via `--print --permission-mode bypassPermissions`)
2. `codex` (`codex exec 'prompt'`)
3. `opencode` (`opencode run --prompt 'prompt'`)
4. `pi` (`pi run --prompt 'prompt'`)

Override by passing `cli: "codex"` (etc.) on the tool call.

## Safety

- The CLI runs in an **ephemeral workspace** under
  `~/.oxenclaw/skill-workspaces/coding-agent-*/`. Anything written
  outside the workspace is the CLI's responsibility (we don't sandbox
  filesystem access — the operator opts into trust by installing the
  CLI).
- **Network**: not blocked at the skill level. Combine with the
  isolation backend if stricter sandboxing is required.
- Pair with the approval gate (`gated_tool`) when running against a
  real codebase the operator cares about.
