"""ClawHub HTTP client.

Mirrors openclaw `src/infra/clawhub.ts`. Endpoints used:
- GET /api/v1/search?q=&limit=        — full-text skill search
- GET /api/v1/skills?limit=            — list/browse
- GET /api/v1/skills/{slug}            — detail
- GET /api/v1/download?slug=&version=  — fetch ZIP archive

Token resolution order matches openclaw:
1. constructor `token=` argument
2. $CLAWHUB_TOKEN env var (or $OPENCLAW_CLAWHUB_TOKEN)
3. ~/.config/clawhub/config.json `token` field
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import aiohttp

from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("clawhub.client")

DEFAULT_BASE_URL = "https://clawhub.ai"
DEFAULT_TIMEOUT = 30.0


class ClawHubError(Exception):
    """Wire-level or protocol failure talking to ClawHub."""

    def __init__(self, status: int | None, path: str, message: str) -> None:
        super().__init__(f"clawhub {path} → status={status}: {message}")
        self.status = status
        self.path = path


def _config_token_from_disk() -> str | None:
    home = Path.home()
    candidates = [
        home / ".config" / "clawhub" / "config.json",
        home / ".config" / "openclaw" / "clawhub.json",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        token = data.get("token")
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


def resolve_token(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    for env_key in ("CLAWHUB_TOKEN", "OPENCLAW_CLAWHUB_TOKEN"):
        v = os.environ.get(env_key)
        if v and v.strip():
            return v.strip()
    return _config_token_from_disk()


def sha256_integrity(data: bytes) -> str:
    return f"sha256-{hashlib.sha256(data).hexdigest()}"


class ClawHubClient:
    """Async client for the ClawHub REST API."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = resolve_token(token)
        self._timeout = timeout
        self._http: aiohttp.ClientSession | None = http_session
        self._owns_session = http_session is None

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession()
            self._owns_session = True
        return self._http

    async def aclose(self) -> None:
        if self._owns_session and self._http is not None:
            await self._http.close()
            self._http = None

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _get_json(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> Any:
        url = f"{self._base_url}{path}"
        session = await self._ensure_session()
        async with session.get(
            url,
            params={k: v for k, v in (params or {}).items() if v is not None},
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=self._timeout),
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise ClawHubError(resp.status, path, text[:500])
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ClawHubError(resp.status, path, f"invalid JSON: {exc}") from exc

    # ── public API ────────────────────────────────────────────────────────

    async def search_skills(
        self, query: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        data = await self._get_json(
            "/api/v1/search",
            params={"q": query.strip(), "limit": str(limit) if limit else None},
        )
        results = data.get("results") if isinstance(data, dict) else None
        return list(results or [])

    async def list_skills(self, *, limit: int | None = None) -> dict[str, Any]:
        data = await self._get_json(
            "/api/v1/skills",
            params={"limit": str(limit) if limit else None},
        )
        return data if isinstance(data, dict) else {}

    async def fetch_skill_detail(self, slug: str) -> dict[str, Any]:
        # Slug is path-encoded by aiohttp internally when we use the URL
        # template above; safe characters are kept.
        path = f"/api/v1/skills/{slug}"
        return await self._get_json(path)

    async def download_skill_archive(
        self, slug: str, *, version: str | None = None
    ) -> tuple[bytes, str]:
        """Return (archive_bytes, integrity_string)."""
        url = f"{self._base_url}/api/v1/download"
        params: dict[str, str] = {"slug": slug}
        if version:
            params["version"] = version
        session = await self._ensure_session()
        async with session.get(
            url,
            params=params,
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=self._timeout),
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise ClawHubError(resp.status, "/api/v1/download", body[:500])
            data = await resp.read()
        return data, sha256_integrity(data)
