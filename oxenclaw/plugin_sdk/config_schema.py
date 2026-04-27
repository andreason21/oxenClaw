"""Base Pydantic config models shared by all plugins.

Port of openclaw `src/plugin-sdk/config-schema.ts`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    account_id: str
    display_name: str | None = None


DmPolicy = Literal["pairing", "open"]


class ChannelConfig(BaseModel):
    """Common shape every channel config inherits. Subclasses add channel-specific fields."""

    model_config = ConfigDict(extra="allow")

    accounts: list[AccountConfig] = Field(default_factory=list)
    allow_from: list[str] = Field(default_factory=list)
    dm_policy: DmPolicy = "pairing"


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    # `id` is optional: the dict key in `providers.<id>` is the source of
    # truth. Kept as a field for backward compatibility with examples
    # that spell it out explicitly.
    id: str | None = None


class AgentChannelRouting(BaseModel):
    model_config = ConfigDict(extra="allow")

    allow_from: list[str] = Field(default_factory=list)


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    # `id` is optional: the dict key in `agents.<id>` is the source of
    # truth (every dispatcher/registry consumer uses the dict key, not
    # this field). Kept declared so explicit `id:` lines still validate.
    id: str | None = None
    channels: dict[str, AgentChannelRouting] = Field(default_factory=dict)
    provider: str | None = None


class MemoryPrivacyConfig(BaseModel):
    """Privacy / redaction settings for the memory pipeline."""

    model_config = ConfigDict(extra="allow")

    redact_level: Literal["off", "light", "strict"] = "light"
    allow_globs: list[str] = Field(default_factory=list)
    deny_globs: list[str] = Field(
        default_factory=lambda: ["*.log", "secrets/**", "credentials/**"]
    )
    min_file_size: int = 0
    max_file_size: int = 1_048_576  # 1 MiB


class MemoryConfig(BaseModel):
    """Top-level ``memory:`` section of config.yaml."""

    model_config = ConfigDict(extra="allow")

    privacy: MemoryPrivacyConfig = Field(default_factory=MemoryPrivacyConfig)


class RootConfig(BaseModel):
    """Top-level config.yaml shape."""

    model_config = ConfigDict(extra="allow")

    channels: dict[str, ChannelConfig] = Field(default_factory=dict)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    # Section parsed by `oxenclaw.clawhub.registries.ClawHubRegistries`.
    # Kept as a free-form dict here so the plugin SDK doesn't depend on
    # the clawhub package (avoids import cycles); the gateway pulls it.
    clawhub: dict | None = None  # type: ignore[type-arg]
