"""Multiple ClawHub registries (e.g. public hub + verified internal mirror).

Operators describe registries declaratively in config.yaml:

    clawhub:
      default: corp-mirror           # name of the registry to use by default
      registries:
        - name: corp-mirror
          url: https://clawhub.corp.example.com
          token_env: CORP_CLAWHUB_TOKEN
          trust: mirror               # 'official' | 'mirror' | 'community'
        - name: public
          url: https://clawhub.ai
          trust: official

Higher-level code (`MultiRegistryClient`, `SkillInstaller`) takes an optional
`registry=<name>` argument; if omitted the default is used. Each registry
keeps its own `ClawHubClient`, so credentials don't leak across hubs.

Design notes:

- We never *fan out* search across all registries by default — that would
  surprise operators who deploy a private mirror precisely to avoid
  reaching the public hub. Cross-registry browse is opt-in via a flag
  (e.g. `--all-registries` on the CLI).
- The `trust` level is recorded on every install so the dashboard can
  visually distinguish skills sourced from `community` (lower trust)
  from `mirror`/`official`.
- A registry is "verified" by virtue of being explicitly listed in the
  user's config.yaml. We don't ship any allowlist override mechanism —
  if operators want to lock down to a single internal mirror, they
  remove `public` from their config.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oxenclaw.clawhub.client import DEFAULT_BASE_URL, ClawHubClient
from oxenclaw.plugin_sdk.runtime_env import get_logger
from oxenclaw.plugin_sdk.skill_source_contract import (
    SKILL_SOURCE_ENTRY_POINT_GROUP,
    SkillSourcePlugin,
)

logger = get_logger("clawhub.registries")

TrustLevel = Literal["official", "mirror", "community"]
RegistryKind = Literal["clawhub", "plugin"]


class RegistryConfig(BaseModel):
    """One entry in `clawhub.registries`.

    Two kinds:
      * `kind: clawhub` (default) — speaks the standard ClawHub HTTPS
        API. `url` is required; `token` / `token_env` optionally
        authenticate.
      * `kind: plugin` — resolves to a `SkillSourcePlugin` loaded from
        the `oxenclaw.skill_sources` entry-point group at runtime.
        `plugin` names the entry; `options` is passed to the plugin's
        constructor and is plugin-specific (typically holds a git
        URL, SSH key path, etc.). `url` is ignored when present.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    url: str | None = None
    token: str | None = None
    token_env: str | None = None
    trust: TrustLevel = "mirror"
    kind: RegistryKind = "clawhub"
    plugin: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_kind_fields(self) -> RegistryConfig:
        if self.kind == "clawhub" and not self.url:
            raise ValueError(
                f"registry {self.name!r}: kind=clawhub requires a url"
            )
        if self.kind == "plugin" and not self.plugin:
            raise ValueError(
                f"registry {self.name!r}: kind=plugin requires a `plugin` "
                "name pointing at an `oxenclaw.skill_sources` entry point"
            )
        return self

    def resolve_token(self) -> str | None:
        """Static `token` first, then `token_env` lookup."""
        if self.token:
            return self.token
        if self.token_env:
            v = os.environ.get(self.token_env)
            if v and v.strip():
                return v.strip()
        return None


class ClawHubRegistries(BaseModel):
    """`clawhub:` section of config.yaml."""

    model_config = ConfigDict(extra="forbid")

    default: str | None = None
    registries: list[RegistryConfig] = Field(default_factory=list)

    def resolved_default(self) -> str:
        if self.default:
            return self.default
        if self.registries:
            return self.registries[0].name
        return "public"

    def get(self, name: str) -> RegistryConfig | None:
        for r in self.registries:
            if r.name == name:
                return r
        return None

    def names(self) -> list[str]:
        return [r.name for r in self.registries]


def builtin_public_registry() -> RegistryConfig:
    """Fallback registry when config.yaml has none configured."""
    return RegistryConfig(name="public", url=DEFAULT_BASE_URL, trust="official")


def normalise(cfg: ClawHubRegistries | None) -> ClawHubRegistries:
    """Ensure at least one registry exists. Operators that want to lock down
    to a private mirror still need to declare it; we do not silently fall
    back to the public hub when they explicitly listed only mirrors."""
    if cfg is None or not cfg.registries:
        return ClawHubRegistries(default="public", registries=[builtin_public_registry()])
    return cfg


class PluginNotFoundError(KeyError):
    """Raised when a registry of `kind: plugin` references an
    `oxenclaw.skill_sources` entry point that isn't installed."""


def _load_skill_source_plugin(
    plugin_name: str, options: dict[str, Any]
) -> SkillSourcePlugin:
    """Resolve `plugin_name` against the `oxenclaw.skill_sources`
    entry-point group, instantiate it with the per-registry options,
    and verify it satisfies `SkillSourcePlugin`.

    Test seam: `entries` argument lets unit tests inject a fake
    iterable of entry points instead of touching the real
    `importlib.metadata` registry.
    """
    from importlib.metadata import entry_points

    matches = [
        ep
        for ep in entry_points(group=SKILL_SOURCE_ENTRY_POINT_GROUP)
        if ep.name == plugin_name
    ]
    if not matches:
        raise PluginNotFoundError(
            f"skill-source plugin {plugin_name!r} is not installed "
            f"(no entry under {SKILL_SOURCE_ENTRY_POINT_GROUP!r}). "
            f"Install the package providing it, or remove the registry "
            f"from config.yaml."
        )
    target = matches[0].load()
    try:
        instance = target(options=options)
    except TypeError as exc:
        raise TypeError(
            f"skill-source plugin {plugin_name!r}: {target.__name__} "
            f"must accept `options: dict` as a keyword argument ({exc})"
        ) from exc
    if not isinstance(instance, SkillSourcePlugin):
        raise TypeError(
            f"skill-source plugin {plugin_name!r}: {type(instance).__name__} "
            "does not satisfy the SkillSourcePlugin protocol "
            "(missing one of search_skills/list_skills/fetch_skill_detail/"
            "download_skill_archive/aclose)"
        )
    logger.info(
        "skill-source plugin loaded: %s -> %s",
        plugin_name,
        type(instance).__module__ + "." + type(instance).__name__,
    )
    return instance


class MultiRegistryClient:
    """Lazily-cached client per registry name.

    Each registry resolves to either a `ClawHubClient` (for
    `kind: clawhub`) or a third-party `SkillSourcePlugin` (for
    `kind: plugin`). Both satisfy the same async interface, so
    downstream code (`SkillInstaller`, `skills.*` JSON-RPC, the CLI)
    is agnostic to which kind it's talking to.

    All client lookups go through this helper so each invocation can target
    a different registry without leaking credentials.
    """

    def __init__(self, cfg: ClawHubRegistries | None) -> None:
        self._cfg = normalise(cfg)
        self._clients: dict[str, SkillSourcePlugin] = {}

    @property
    def config(self) -> ClawHubRegistries:
        return self._cfg

    def names(self) -> list[str]:
        return self._cfg.names()

    def trust(self, name: str) -> TrustLevel:
        entry = self._cfg.get(name)
        return entry.trust if entry is not None else "community"

    def get_client(self, name: str | None = None) -> SkillSourcePlugin:
        chosen = name or self._cfg.resolved_default()
        if chosen in self._clients:
            return self._clients[chosen]
        entry = self._cfg.get(chosen)
        if entry is None:
            raise KeyError(
                f"registry {chosen!r} not configured (known: {', '.join(self.names()) or 'none'})"
            )
        client: SkillSourcePlugin
        if entry.kind == "plugin":
            assert entry.plugin is not None  # validated by RegistryConfig
            client = _load_skill_source_plugin(entry.plugin, entry.options)
        else:
            assert entry.url is not None  # validated by RegistryConfig
            client = ClawHubClient(base_url=entry.url, token=entry.resolve_token())
        self._clients[chosen] = client
        return client

    def view(self) -> list[dict]:  # type: ignore[type-arg]
        """Render registries for `skills.registries` RPC. Tokens never surface."""
        return [
            {
                "name": r.name,
                "url": r.url or "",
                "trust": r.trust,
                "kind": r.kind,
                "plugin": r.plugin,
                "default": r.name == self._cfg.resolved_default(),
                "has_token": r.resolve_token() is not None,
            }
            for r in self._cfg.registries
        ]

    def iter_clients(self) -> Iterable[tuple[str, SkillSourcePlugin]]:
        """Used by the optional all-registries fan-out search."""
        for r in self._cfg.registries:
            yield r.name, self.get_client(r.name)

    async def aclose(self) -> None:
        """Close every materialised client. Plugins MUST implement
        `aclose()` per the protocol; failures are logged but never
        raised so a single broken plugin doesn't keep others alive."""
        for name, client in list(self._clients.items()):
            try:
                await client.aclose()
            except Exception:
                logger.exception("registry %r: aclose failed", name)
        self._clients.clear()

    async def aclose(self) -> None:
        for c in self._clients.values():
            await c.aclose()
        self._clients.clear()
