"""config.* RPCs: expose and reload the YAML config.

Writes are deliberately out of scope — file-edit semantics are risky at RPC
boundary (merging, ordering, comments). Editors + `config.reload` is the
recommended workflow.
"""

from __future__ import annotations

from collections.abc import Callable

from sampyclaw.config import load_config
from sampyclaw.gateway.router import Router
from sampyclaw.plugin_sdk.config_schema import RootConfig

ConfigSink = Callable[[RootConfig], None]


def register_config_methods(router: Router, *, sink: ConfigSink | None = None) -> None:
    """Register config.get / config.reload.

    `sink` is called on reload with the fresh RootConfig so the caller can
    re-wire dispatchers, channel registries, etc. Passing None means reload
    just re-reads from disk and returns it — nothing downstream is refreshed.
    """

    @router.method("config.get")
    async def _get(_: dict) -> dict:  # type: ignore[type-arg]
        return load_config().model_dump()

    @router.method("config.reload")
    async def _reload(_: dict) -> dict:  # type: ignore[type-arg]
        cfg = load_config()
        if sink is not None:
            sink(cfg)
        return {"reloaded": True, "channels": sorted(cfg.channels), "agents": sorted(cfg.agents)}
