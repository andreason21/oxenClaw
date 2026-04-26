---
name: github
description: "Interact with GitHub repos / issues / PRs by delegating to the `gh` CLI. Auth via GH_TOKEN env."
homepage: https://github.com/oxenclaw
openclaw:
  emoji: "🐙"
  requires:
    anyBins: ["gh"]
  install:
    - id: gh-brew
      kind: brew
      package: "gh"
      bins: ["gh"]
      label: "Install GitHub CLI (Homebrew)"
    - id: gh-apt
      kind: apt
      package: "gh"
      bins: ["gh"]
      label: "Install GitHub CLI (apt)"
  env_overrides:
    GH_TOKEN: "$GH_TOKEN"
---

# github

Wraps the `gh` CLI. The model passes a sub-command + args; the tool runs
`gh <verb> <args...>` in an ephemeral workspace and returns stdout.

Curated verbs (allow-list at the tool level so the model can't run
arbitrary `gh` commands):

| verb | description |
|---|---|
| `issue list` | list issues in a repo |
| `issue view` | show an issue |
| `pr list` | list PRs |
| `pr view` | show a PR |
| `pr diff` | show a PR's diff |
| `repo view` | show repo metadata |
| `api` | call GitHub REST API endpoint (read-only) |
