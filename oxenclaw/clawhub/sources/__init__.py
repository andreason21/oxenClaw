"""Skill discovery sources — pluggable backends behind a single interface.

`SkillSource` is the contract: every backend (ClawHub HTTP, GitHub repo
search, an aggregated index URL) implements `search`, `fetch`,
`inspect`. `parallel_search_sources` fans out across all configured
sources with timeouts + dedup so the LLM gets the best match without
having to know which backend it came from.
"""

from oxenclaw.clawhub.sources.base import SkillBundle, SkillRef, SkillSource

__all__ = ["SkillBundle", "SkillRef", "SkillSource"]
