"""ClawHub-backed `SkillSource`.

Thin adapter over the existing `MultiRegistryClient` so the legacy HTTP
search path becomes one source among many. The async client gets driven
synchronously (via `asyncio.run`) so this source plugs into the
`ThreadPoolExecutor` fan-out in `parallel_search.py`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from oxenclaw.clawhub.frontmatter import SkillManifest, SkillManifestError
from oxenclaw.clawhub.sources.base import SkillBundle, SkillRef, SkillSource

logger = logging.getLogger(__name__)


class ClawHubSource(SkillSource):
    """Search/fetch ClawHub via an existing `MultiRegistryClient`."""

    source_id = "clawhub"

    def __init__(self, registries: Any) -> None:
        # `registries` is a `MultiRegistryClient` (or duck-compatible).
        self._registries = registries
        # Trust level: take from the default registry's configured trust.
        try:
            default_name = registries.config.resolved_default()
            self.trust_level = registries.trust(default_name)
        except Exception:
            self.trust_level = "community"

    def _client(self):  # type: ignore[no-untyped-def]
        return self._registries.get_client()

    @staticmethod
    def _run(coro):  # type: ignore[no-untyped-def]
        """Run an async coroutine from a sync context.

        The skill resolver is called from both async and sync paths.
        Within an active loop callers should not hit this wrapper —
        they should use the async variants of the underlying methods.
        """
        try:
            return asyncio.run(coro)
        except RuntimeError:
            # Already inside a loop — schedule on a fresh loop in a thread.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(asyncio.run, coro)
                return fut.result()

    def search(self, query: str, limit: int = 10) -> list[SkillRef]:
        try:
            results: list[dict[str, Any]] = (
                self._run(self._client().search_skills(query, limit=limit)) or []
            )
        except Exception as exc:
            logger.debug("clawhub search failed: %s", exc)
            return []
        out: list[SkillRef] = []
        for r in results:
            slug = r.get("slug") or r.get("name") or ""
            if not slug:
                continue
            out.append(
                SkillRef(
                    id=slug,
                    slug=slug,
                    source_id=self.source_id,
                    description=r.get("description", "") or "",
                    trust_level=self.trust_level,
                )
            )
        return out

    def fetch(self, skill_id: str) -> SkillBundle:
        # Full fetch goes through the existing SkillInstaller path —
        # this method is intentionally minimal so callers prefer the
        # installer when they want to land the skill on disk. We just
        # surface the manifest stub here.
        manifest = self.inspect(skill_id)
        return SkillBundle(manifest=manifest, body="", files={})

    def inspect(self, skill_id: str) -> SkillManifest:
        try:
            detail = self._run(self._client().fetch_skill_detail(skill_id)) or {}
        except Exception as exc:
            raise SkillManifestError(f"clawhub detail fetch failed: {exc}") from exc
        try:
            return SkillManifest.model_validate(
                {
                    "name": detail.get("slug") or skill_id,
                    "description": detail.get("description") or "",
                }
            )
        except Exception as exc:
            raise SkillManifestError(f"could not parse clawhub manifest: {exc}") from exc


__all__ = ["ClawHubSource"]
