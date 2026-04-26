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
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sampyclaw.clawhub.client import DEFAULT_BASE_URL, ClawHubClient

TrustLevel = Literal["official", "mirror", "community"]


class RegistryConfig(BaseModel):
    """One entry in `clawhub.registries`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    url: str
    token: str | None = None
    token_env: str | None = None
    trust: TrustLevel = "mirror"

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


class MultiRegistryClient:
    """Lazily-cached ClawHubClient per registry name.

    All client lookups go through this helper so each invocation can target
    a different registry without leaking credentials.
    """

    def __init__(self, cfg: ClawHubRegistries | None) -> None:
        self._cfg = normalise(cfg)
        self._clients: dict[str, ClawHubClient] = {}

    @property
    def config(self) -> ClawHubRegistries:
        return self._cfg

    def names(self) -> list[str]:
        return self._cfg.names()

    def trust(self, name: str) -> TrustLevel:
        entry = self._cfg.get(name)
        return entry.trust if entry is not None else "community"

    def get_client(self, name: str | None = None) -> ClawHubClient:
        chosen = name or self._cfg.resolved_default()
        if chosen in self._clients:
            return self._clients[chosen]
        entry = self._cfg.get(chosen)
        if entry is None:
            raise KeyError(
                f"registry {chosen!r} not configured (known: {', '.join(self.names()) or 'none'})"
            )
        client = ClawHubClient(base_url=entry.url, token=entry.resolve_token())
        self._clients[chosen] = client
        return client

    def view(self) -> list[dict]:  # type: ignore[type-arg]
        """Render registries for `skills.registries` RPC. Tokens never surface."""
        return [
            {
                "name": r.name,
                "url": r.url,
                "trust": r.trust,
                "default": r.name == self._cfg.resolved_default(),
                "has_token": r.resolve_token() is not None,
            }
            for r in self._cfg.registries
        ]

    def iter_clients(self) -> Iterable[tuple[str, ClawHubClient]]:
        """Used by the optional all-registries fan-out search."""
        for r in self._cfg.registries:
            yield r.name, self.get_client(r.name)

    async def aclose(self) -> None:
        for c in self._clients.values():
            await c.aclose()
        self._clients.clear()
