"""ClawHub — remote skill registry client + installer + loader.

Mirrors openclaw's `src/agents/skills-clawhub.ts` + `src/infra/clawhub.ts`
on top of Python aiohttp. Skills are installed under
`~/.oxenclaw/skills/<slug>/` with the canonical `SKILL.md` at the root.
"""

from oxenclaw.clawhub.client import (
    DEFAULT_BASE_URL,
    ClawHubClient,
    ClawHubError,
)
from oxenclaw.clawhub.frontmatter import (
    VALID_SLUG_RE,
    SkillManifest,
    SkillManifestError,
    parse_skill_file,
    parse_skill_text,
)
from oxenclaw.clawhub.installer import (
    InstallError,
    SkillInstaller,
)
from oxenclaw.clawhub.loader import InstalledSkill, format_skills_for_prompt, load_installed_skills
from oxenclaw.clawhub.lockfile import LockEntry, Lockfile, OriginMetadata
from oxenclaw.clawhub.registries import (
    ClawHubRegistries,
    MultiRegistryClient,
    RegistryConfig,
    TrustLevel,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "VALID_SLUG_RE",
    "ClawHubClient",
    "ClawHubError",
    "ClawHubRegistries",
    "InstallError",
    "InstalledSkill",
    "LockEntry",
    "Lockfile",
    "MultiRegistryClient",
    "OriginMetadata",
    "RegistryConfig",
    "SkillInstaller",
    "SkillManifest",
    "SkillManifestError",
    "TrustLevel",
    "format_skills_for_prompt",
    "load_installed_skills",
    "parse_skill_file",
    "parse_skill_text",
]
