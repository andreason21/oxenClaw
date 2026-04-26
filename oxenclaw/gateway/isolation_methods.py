"""isolation.* RPCs — surface backend availability + smoke-tests."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from oxenclaw.gateway.router import Router
from oxenclaw.security.isolation import (
    IsolationPolicy,
    available_backends,
    default_registry,
)


class _SmokeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: str | None = None  # null -> auto-resolve


def register_isolation_methods(router: Router) -> None:
    @router.method("isolation.backends")
    async def _backends(_: dict) -> dict[str, Any]:  # type: ignore[type-arg]
        avail = await available_backends()
        # Strength order, strongest first.
        all_backends = ["container", "bwrap", "subprocess", "inprocess"]
        return {
            "available": avail,
            "all_known": all_backends,
            "strongest": next((n for n in all_backends if n in avail), "inprocess"),
        }

    @router.method("isolation.smoke", _SmokeParams)
    async def _smoke(p: _SmokeParams) -> dict[str, Any]:  # type: ignore[type-arg]
        """Run `echo isolation-smoke` through a backend; report timing + scrub.

        Smoke tests backend mechanics (exec → capture → cleanup), not isolation
        enforcement, so the policy opts out of network/filesystem strictness so
        the subprocess backend will run.
        """
        policy = IsolationPolicy(
            backend=p.backend,  # type: ignore[arg-type]
            timeout_seconds=5.0,
            network=True,
            filesystem="full",
        )
        backend = await default_registry().resolve(policy)
        result = await backend.run(["echo", "isolation-smoke"], policy=policy)
        return {
            "backend": result.backend,
            "ok": result.ok,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "duration_seconds": round(result.duration_seconds, 3),
            "timed_out": result.timed_out,
        }
