"""ClawHub — remote skill registry client + installer + loader.

Mirrors openclaw's `src/agents/skills-clawhub.ts` + `src/infra/clawhub.ts`
on top of Python aiohttp. Skills are installed under
`~/.sampyclaw/skills/<slug>/` with the canonical `SKILL.md` at the root.
"""

from sampyclaw.clawhub.client import (
    DEFAULT_BASE_URL,
    ClawHubClient,
    ClawHubError,
)
from sampyclaw.clawhub.frontmatter import (
    VALID_SLUG_RE,
    SkillManifest,
    SkillManifestError,
    parse_skill_file,
    parse_skill_text,
)
from sampyclaw.clawhub.installer import (
    InstallError,
    SkillInstaller,
)
from sampyclaw.clawhub.loader import InstalledSkill, format_skills_for_prompt, load_installed_skills
from sampyclaw.clawhub.lockfile import LockEntry, Lockfile, OriginMetadata
from sampyclaw.clawhub.registries import (
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
