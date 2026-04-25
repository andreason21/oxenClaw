"""channels.* RPCs bound to a channel-agnostic ChannelRouter."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from sampyclaw.channels import ChannelRouter
from sampyclaw.gateway.router import Router


class _ProbeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: str
    account_id: str


def register_channels_methods(router: Router, channel_router: ChannelRouter) -> None:
    @router.method("channels.list")
    async def _list(_: dict) -> dict:  # type: ignore[type-arg]
        return channel_router.channels_by_id()

    @router.method("channels.probe", _ProbeParams)
    async def _probe(p: _ProbeParams) -> dict:  # type: ignore[type-arg]
        result = await channel_router.probe(p.channel, p.account_id)
        return result.model_dump()
