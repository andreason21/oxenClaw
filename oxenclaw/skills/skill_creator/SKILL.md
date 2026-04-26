---
name: skill-creator
description: "Scaffold a new SKILL.md (and an optional Python tool stub) into ~/.oxenclaw/skills/<slug>/. Validates frontmatter."
homepage: https://github.com/oxenclaw
openclaw:
  emoji: "🛠️"
---

# skill-creator

Meta-skill for bootstrapping new skills:

1. The agent describes a skill (name, description, optional install/env).
2. The tool builds `~/.oxenclaw/skills/<slug>/SKILL.md` with valid
   frontmatter that the loader can parse.
3. Optionally writes a Python tool stub in the same dir.

The skill is then discoverable by `load_installed_skills()` on next
agent restart (or after a clawhub refresh).
