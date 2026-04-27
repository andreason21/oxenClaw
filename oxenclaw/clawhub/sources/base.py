"""SkillSource ABC + shared dataclasses.

Each backend (`ClawHubSource`, `GitHubSource`, `IndexSource`) implements
this contract so callers can fan out across all of them in parallel.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from oxenclaw.clawhub.frontmatter import SkillManifest


@dataclass(frozen=True)
class SkillRef:
    """Lightweight pointer to a discoverable skill."""

    id: str
    slug: str
    source_id: str
    description: str = ""
    trust_level: str = "community"  # "official" | "mirror" | "community"
    tags: tuple[str, ...] = ()


@dataclass
class SkillBundle:
    """A fully-fetched skill — manifest + body + supporting files."""

    manifest: SkillManifest
    body: str
    files: dict[str, bytes] = field(default_factory=dict)


class SkillSource(ABC):
    """Pluggable skill backend."""

    source_id: str = "unknown"
    trust_level: str = "community"

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> list[SkillRef]:
        """Return up to `limit` candidates matching `query`."""

    @abstractmethod
    def fetch(self, skill_id: str) -> SkillBundle:
        """Return the full bundle (manifest + body + files)."""

    @abstractmethod
    def inspect(self, skill_id: str) -> SkillManifest:
        """Cheaper than fetch — just the manifest, no body/files."""


__all__ = ["SkillBundle", "SkillRef", "SkillSource"]
