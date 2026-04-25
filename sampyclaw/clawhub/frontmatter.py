"""SKILL.md YAML frontmatter parser.

Mirrors openclaw `src/agents/skills/frontmatter.ts`. A SKILL.md file looks
like::

    ---
    name: gifgrep
    description: Search GIF providers, download stills, etc.
    homepage: https://gifgrep.com
    metadata:
      openclaw:
        emoji: 🧲
        requires:
          bins: [gifgrep]
        install:
          - id: brew
            kind: brew
            formula: example/tap/gifgrep
            bins: [gifgrep]
    ---

    # Body markdown that the agent reads when activating the skill.
    ...

We expose the parsed frontmatter as a `SkillManifest` Pydantic model and
return both manifest + body separately. Body content stays unparsed
(markdown is consumed by the agent, not by us).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

# kebab-case-ish slug rule from openclaw `skills-clawhub.ts:VALID_SLUG_PATTERN`.
VALID_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$", re.IGNORECASE)

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n?(.*)$",
    re.DOTALL,
)


InstallKind = Literal["brew", "node", "go", "uv", "download"]


class SkillRequires(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    bins: list[str] = Field(default_factory=list)
    any_bins: list[str] = Field(default_factory=list, alias="anyBins")
    env: list[str] = Field(default_factory=list)
    config: list[str] = Field(default_factory=list)


class SkillInstallSpec(BaseModel):
    """One element of `metadata.openclaw.install`. We DO NOT execute these.

    They're surfaced to the user as "this skill needs <X> on your PATH" so
    they can install via their own package manager. Auto-running brew/npm/go
    inside the gateway would be a glaring security hole.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str | None = None
    kind: InstallKind | str
    label: str | None = None
    bins: list[str] = Field(default_factory=list)
    os: list[str] = Field(default_factory=list)
    formula: str | None = None
    package: str | None = None
    module: str | None = None
    url: str | None = None
    archive: str | None = None
    extract: bool | None = None
    strip_components: int | None = Field(default=None, alias="stripComponents")
    target_dir: str | None = Field(default=None, alias="targetDir")


class SkillOpenClawMetadata(BaseModel):
    """`metadata.openclaw` block. clawhub treats this as the source of truth."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    emoji: str | None = None
    skill_key: str | None = Field(default=None, alias="skillKey")
    primary_env: str | None = Field(default=None, alias="primaryEnv")
    always: bool = False
    os: list[str] = Field(default_factory=list)
    requires: SkillRequires = Field(default_factory=SkillRequires)
    install: list[SkillInstallSpec] = Field(default_factory=list)


class SkillManifest(BaseModel):
    """Parsed YAML frontmatter from SKILL.md.

    Required: `name`, `description`. Everything else (homepage, openclaw
    metadata, raw extra fields) is optional and preserved.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    description: str
    homepage: str | None = None
    license: str | None = None
    version: str | None = None
    openclaw: SkillOpenClawMetadata = Field(default_factory=SkillOpenClawMetadata)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name is required")
        if not VALID_SLUG_RE.match(v):
            raise ValueError(
                f"name {v!r} must be alphanumeric + hyphens, "
                f"matching {VALID_SLUG_RE.pattern}"
            )
        return v


class SkillManifestError(ValueError):
    """Raised when SKILL.md frontmatter is missing or malformed."""


def parse_skill_text(content: str) -> tuple[SkillManifest, str]:
    """Parse a SKILL.md document into (manifest, body).

    Body is the markdown after the closing `---`, stripped of a leading newline.
    """
    if not content.lstrip().startswith("---"):
        raise SkillManifestError("SKILL.md must begin with a `---` frontmatter delimiter")

    match = _FRONTMATTER_RE.match(content.lstrip())
    if match is None:
        raise SkillManifestError("could not locate closing `---` delimiter")

    raw_yaml, body = match.group(1), match.group(2)
    try:
        data = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as exc:
        raise SkillManifestError(f"malformed YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillManifestError("frontmatter root must be a mapping")

    # Promote `metadata.openclaw` → top-level `openclaw` for our model shape.
    metadata = data.pop("metadata", None)
    if isinstance(metadata, dict) and "openclaw" in metadata:
        data["openclaw"] = metadata["openclaw"]

    try:
        manifest = SkillManifest.model_validate(data)
    except Exception as exc:
        raise SkillManifestError(f"invalid skill manifest: {exc}") from exc
    return manifest, body.lstrip("\n")


def parse_skill_file(path: str | Path) -> tuple[SkillManifest, str]:
    text = Path(path).read_text(encoding="utf-8")
    return parse_skill_text(text)


def is_valid_slug(value: str) -> bool:
    return bool(VALID_SLUG_RE.match(value or ""))


def serialise_install_specs(specs: list[SkillInstallSpec]) -> list[dict[str, Any]]:
    """Render install specs in a stable, JSON-friendly form for UIs."""
    return [s.model_dump(by_alias=True, exclude_none=True) for s in specs]
