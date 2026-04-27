"""Gateway service-restart exit code.

Mirrors `hermes-agent/gateway/restart.py:7`. A supervisor (systemd,
docker, etc.) restarts the gateway when it sees this code, while
treating any other non-zero exit as a fatal error.

Picked from BSD `EX_TEMPFAIL` (75): traditionally "the user is invited
to retry the operation later".
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

GATEWAY_SERVICE_RESTART_EXIT_CODE = 75  # BSD EX_TEMPFAIL


class _RestartParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


def register_restart_method(router: Any, server: Any) -> None:
    """Register `gateway.restart` so a privileged client can ask the
    server to drain + exit with `GATEWAY_SERVICE_RESTART_EXIT_CODE`.

    `server` must expose `request_restart()` (`GatewayServer` does).
    """

    @router.method("gateway.restart", _RestartParams)
    async def _restart(_: _RestartParams) -> dict[str, Any]:  # type: ignore[type-arg]
        server.request_restart()
        return {"requested": True, "exit_code": GATEWAY_SERVICE_RESTART_EXIT_CODE}


__all__ = [
    "GATEWAY_SERVICE_RESTART_EXIT_CODE",
    "register_restart_method",
]
