"""Backend registry + auto-fallback selection.

`resolve_backend(policy)` returns the strongest backend that satisfies
`policy.backend` (the hint is a *minimum* — we will use a stronger backend
if available unless the hint forces a specific one).

Strength order (strongest first):  container > bwrap > subprocess > inprocess
"""

from __future__ import annotations

from oxenclaw.plugin_sdk.runtime_env import get_logger
from oxenclaw.security.isolation.bwrap import BubblewrapBackend
from oxenclaw.security.isolation.container import ContainerBackend
from oxenclaw.security.isolation.inprocess import InprocessBackend
from oxenclaw.security.isolation.policy import (
    BackendName,
    IsolationBackend,
    IsolationPolicy,
)
from oxenclaw.security.isolation.subprocess_backend import SubprocessBackend

logger = get_logger("security.isolation.registry")

_STRENGTH: dict[BackendName, int] = {
    "inprocess": 0,
    "subprocess": 1,
    "bwrap": 2,
    "container": 3,
}


class BackendRegistry:
    def __init__(self, backends: list[IsolationBackend] | None = None) -> None:
        self._backends: list[IsolationBackend] = backends or [
            ContainerBackend(),
            BubblewrapBackend(),
            SubprocessBackend(),
            InprocessBackend(),
        ]

    @property
    def backends(self) -> list[IsolationBackend]:
        return list(self._backends)

    async def available(self) -> list[BackendName]:
        out: list[BackendName] = []
        for b in self._backends:
            try:
                if await b.is_available():
                    out.append(b.name)
            except Exception:
                pass
        return out

    async def resolve(self, policy: IsolationPolicy) -> IsolationBackend:
        avail = await self.available()
        if policy.backend is not None:
            # Explicit pin — must be available, no upgrade.
            for b in self._backends:
                if b.name == policy.backend and policy.backend in avail:
                    return b
            # Fall through if pinned-but-missing: best-effort downgrade.
            logger.warning(
                "policy pinned backend=%s but not available; falling back",
                policy.backend,
            )

        # Strongest available wins.
        ranked = sorted(avail, key=lambda n: _STRENGTH[n], reverse=True)
        if not ranked:
            return InprocessBackend()  # always-available safety net
        chosen = ranked[0]
        for b in self._backends:
            if b.name == chosen:
                return b
        return InprocessBackend()


_DEFAULT: BackendRegistry | None = None


def default_registry() -> BackendRegistry:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = BackendRegistry()
    return _DEFAULT


async def available_backends() -> list[BackendName]:
    return await default_registry().available()


async def resolve_backend(policy: IsolationPolicy) -> IsolationBackend:
    return await default_registry().resolve(policy)
