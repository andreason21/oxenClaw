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

    id: str


class AgentChannelRouting(BaseModel):
    model_config = ConfigDict(extra="allow")

    allow_from: list[str] = Field(default_factory=list)


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    channels: dict[str, AgentChannelRouting] = Field(default_factory=dict)
    provider: str | None = None


class RootConfig(BaseModel):
    """Top-level config.yaml shape."""

    model_config = ConfigDict(extra="allow")

    channels: dict[str, ChannelConfig] = Field(default_factory=dict)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    # Section parsed by `oxenclaw.clawhub.registries.ClawHubRegistries`.
    # Kept as a free-form dict here so the plugin SDK doesn't depend on
    # the clawhub package (avoids import cycles); the gateway pulls it.
    clawhub: dict | None = None  # type: ignore[type-arg]
