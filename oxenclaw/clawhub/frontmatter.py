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

import logging
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_logger = logging.getLogger("clawhub.frontmatter")

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


class SkillCommand(BaseModel):
    """One auto-registered command in a skill's `commands:` frontmatter.

    Mirrors openclaw's skill-commands convention. When a skill ships a
    `commands` block in its frontmatter, the loader registers each
    entry as a callable LLM tool that runs the named shell template
    via the gateway's shell tool. The skill author writes natural
    language; the model invokes by tool name.

    Example frontmatter:
        commands:
          - name: weather_lookup
            description: "Look up current weather for a city."
            template: 'curl "wttr.in/{city}?format=3"'
            inputs:
              city: { type: string, required: true }

    `template` defaults to "" so the manifest can also carry docs-only
    shorthand entries (parsed from string lines like
    `"/stock_alerts - Check triggered alerts"`). Docs-only entries
    appear in the catalog but are not registered as callable tools —
    `is_runnable` returns False for them so `build_skill_command_tools`
    can skip cleanly.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    description: str
    template: str = ""
    inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30.0, alias="timeoutSeconds")

    @field_validator("name")
    @classmethod
    def _validate_command_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("command name is required")
        if not VALID_SLUG_RE.match(v.replace("_", "-")):
            raise ValueError(f"command name {v!r} must be alphanumeric + underscores/hyphens")
        return v

    @property
    def is_runnable(self) -> bool:
        """True when this command has a shell template to execute.
        Docs-only entries (shorthand string in the manifest) report
        False and should be skipped by the tool builder."""
        return bool(self.template and self.template.strip())


_SHORTHAND_CMD_RE = re.compile(
    r"""^/?           # optional leading slash
        (?P<name>[A-Za-z0-9_][A-Za-z0-9_\-]*)   # command name
        \s*
        (?:[\-:–—]\s*(?P<desc>.+?))?            # optional " - description"
        \s*$""",
    re.VERBOSE,
)


def _coerce_skill_commands(raw: Any) -> Any:
    """Normalise the `commands:` list before pydantic validation.

    Real-world skill manifests on ClawHub mix two shapes:

      - Full dict entries with `name` + `template` (callable, what we
        register as LLM tools).
      - Shorthand strings like `"/stock_alerts - Check triggered alerts"`
        — documentation-only catalog entries with no template.

    Pre-fix, the strict pydantic schema rejected the strings outright
    and the entire `skills.install` RPC failed with a ValidationError.
    Result: the skill folder existed on disk but zero tools were
    registered, leaving the agent with nothing to call. This coercion
    keeps the manifest loadable: dicts pass through, parseable strings
    become docs-only `SkillCommand` entries (template=""), and
    anything we can't parse is dropped with a warning instead of
    nuking the install.
    """
    if not isinstance(raw, list):
        return raw
    out: list[Any] = []
    for entry in raw:
        if isinstance(entry, dict):
            out.append(entry)
            continue
        if isinstance(entry, str):
            m = _SHORTHAND_CMD_RE.match(entry.strip())
            if m is None:
                _logger.warning("skill manifest: dropping unparseable command entry %r", entry)
                continue
            name = m.group("name").replace("-", "_")
            desc = (m.group("desc") or name).strip()
            out.append(
                {
                    "name": name,
                    "description": desc,
                    "template": "",  # docs-only — see SkillCommand.is_runnable
                }
            )
            continue
        _logger.warning(
            "skill manifest: dropping non-dict / non-string command entry %r",
            entry,
        )
    return out


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
    commands: list[SkillCommand] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_commands_block(cls, data: Any) -> Any:
        """Run `commands` through the lenient coercer before per-entry
        validation so mixed-shape manifests don't crash the install."""
        if isinstance(data, dict) and "commands" in data:
            data = dict(data)
            data["commands"] = _coerce_skill_commands(data["commands"])
        return data

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name is required")
        if not VALID_SLUG_RE.match(v):
            raise ValueError(
                f"name {v!r} must be alphanumeric + hyphens, matching {VALID_SLUG_RE.pattern}"
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

    # Promote `metadata.openclaw` → top-level `openclaw` for our model
    # shape. Real-world ClawHub skills published from the Clawdbot
    # publisher use `metadata.clawdbot` instead — treat it as a synonym
    # so `requires.bins` / `install` / `os` get parsed and the compat
    # filter actually applies. Pre-fix, `stock-analysis` (and every
    # other Clawdbot-published skill) reported "no requirements" so
    # the catalog showed it as installable on a machine without `uv`,
    # the user installed it, and then `analyze_stock.py` failed at
    # runtime with no actionable signal.
    metadata = data.pop("metadata", None)
    if isinstance(metadata, dict):
        oc_block = metadata.get("openclaw") or metadata.get("clawdbot")
        if isinstance(oc_block, dict):
            data["openclaw"] = oc_block

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
