"""agents.* RPCs: create/delete an in-memory agent via the shared factory.

Changes here do NOT persist to config.yaml — use `config.reload` plus your
editor for durable agent declarations. This surface is for ephemeral
testing, experimentation, and UIs that want to manage agents at runtime.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from sampyclaw.agents import (
    AgentRegistry,
    SUPPORTED_PROVIDERS,
    UnknownProvider,
    build_agent,
)
from sampyclaw.gateway.router import Router


class _IdParam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str


class _CreateParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    provider: str
    system_prompt: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


def _provider_name(agent) -> str:  # type: ignore[no-untyped-def]
    """Best-effort provider id from an agent instance."""
    cls = type(agent).__name__
    return {
        "EchoAgent": "echo",
        "LocalAgent": "local",
        "PiAgent": "pi",
    }.get(cls, cls)


def _agent_details(agent) -> dict:  # type: ignore[no-untyped-def, type-arg]
    """Pull whatever attributes are available off an agent instance.

    Echo agent has none of these private fields, so optional getattrs.
    """
    tools = getattr(agent, "_tools", None)
    return {
        "id": agent.id,
        "provider": _provider_name(agent),
        "model": getattr(agent, "_model", None),
        "system_prompt": getattr(agent, "_system_prompt", None),
        "base_url": getattr(agent, "_base_url", None),
        "tools": sorted(tools.names()) if tools is not None else [],
    }


def register_agents_methods(router: Router, registry: AgentRegistry) -> None:
    @router.method("agents.list")
    async def _list(_: dict) -> list[str]:  # type: ignore[type-arg]
        return registry.ids()

    @router.method("agents.get", _IdParam)
    async def _get(p: _IdParam) -> dict:  # type: ignore[type-arg]
        agent = registry.get(p.id)
        if agent is None:
            return {"found": False, "id": p.id}
        return {"found": True, **_agent_details(agent)}

    @router.method("agents.providers")
    async def _providers(_: dict) -> list[str]:  # type: ignore[type-arg]
        return list(SUPPORTED_PROVIDERS)

    @router.method("agents.create", _CreateParams)
    async def _create(p: _CreateParams) -> dict:  # type: ignore[type-arg]
        try:
            agent = build_agent(
                agent_id=p.id,
                provider=p.provider,
                system_prompt=p.system_prompt,
                model=p.model,
                base_url=p.base_url,
                api_key=p.api_key,
            )
        except UnknownProvider as exc:
            return {"created": False, "error": str(exc)}
        try:
            registry.register(agent)
        except ValueError as exc:
            return {"created": False, "error": str(exc)}
        return {"created": True, "id": agent.id}

    @router.method("agents.delete", _IdParam)
    async def _delete(p: _IdParam) -> dict:  # type: ignore[type-arg]
        existed = registry.get(p.id) is not None
        if existed:
            registry._agents.pop(p.id, None)  # noqa: SLF001 — RPC owns registry lifecycle
        return {"deleted": existed}
