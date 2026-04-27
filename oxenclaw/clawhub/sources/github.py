"""GitHub-tap-backed `SkillSource`.

Each tap is `owner/repo[#branch]/path` — every `SKILL.md` under `path`
gets indexed. The tree listing comes from the REST endpoint
`GET /repos/{owner}/{repo}/git/trees/{branch}?recursive=1`. We cache
the parsed listing for 5 minutes so a burst of `search` calls doesn't
hammer GitHub.

No auth required for public taps; honour `GITHUB_TOKEN` when present
to lift the rate-limit cap (5000 req/h authenticated vs 60 anon).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from oxenclaw.clawhub.frontmatter import (
    SkillManifest,
    SkillManifestError,
    parse_skill_text,
)
from oxenclaw.clawhub.sources.base import SkillBundle, SkillRef, SkillSource

logger = logging.getLogger(__name__)

DEFAULT_TAPS: tuple[str, ...] = (
    "openai/skills",
    "anthropics/skills",
    "VoltAgent/awesome-agent-skills",
)

_TREE_CACHE_TTL = 300.0


@dataclass
class _TapSpec:
    owner: str
    repo: str
    branch: str
    subpath: str

    @classmethod
    def parse(cls, raw: str) -> "_TapSpec":
        text = raw.strip()
        # Owner/repo always come first; an optional `#branch` clause may
        # be followed by a `/path` (path may contain slashes).
        if "#" in text:
            base, after = text.split("#", 1)
            if "/" in after:
                branch, subpath = after.split("/", 1)
            else:
                branch, subpath = after, ""
            parts = base.split("/", 1)
            if len(parts) < 2:
                raise ValueError(f"invalid tap {raw!r}; expected owner/repo[#branch][/path]")
            owner, repo = parts[0], parts[1]
        else:
            parts = text.split("/", 2)
            if len(parts) < 2:
                raise ValueError(f"invalid tap {raw!r}; expected owner/repo[#branch][/path]")
            owner, repo = parts[0], parts[1]
            branch = ""
            subpath = parts[2] if len(parts) > 2 else ""
        return cls(owner=owner, repo=repo, branch=branch or "main", subpath=subpath)

    @property
    def display(self) -> str:
        b = f"#{self.branch}" if self.branch else ""
        p = f"/{self.subpath}" if self.subpath else ""
        return f"{self.owner}/{self.repo}{b}{p}"


@dataclass
class _TapCache:
    fetched_at: float = 0.0
    refs: list[SkillRef] = field(default_factory=list)
    skill_md_paths: dict[str, str] = field(default_factory=dict)


class GitHubSource(SkillSource):
    """Skill source backed by one or more GitHub taps."""

    source_id = "github"
    trust_level = "community"

    def __init__(
        self,
        taps: tuple[str, ...] | None = None,
        *,
        token: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        raw = taps if taps is not None else DEFAULT_TAPS
        self._taps: list[_TapSpec] = []
        for r in raw:
            try:
                self._taps.append(_TapSpec.parse(r))
            except ValueError as exc:
                logger.warning("ignoring invalid tap: %s", exc)
        self._token = token or os.environ.get("GITHUB_TOKEN") or None
        self._timeout = timeout
        self._cache: dict[str, _TapCache] = {}

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _http_json(self, url: str) -> Any:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "oxenclaw-skills",
                **({"Authorization": f"Bearer {self._token}"} if self._token else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            logger.debug("github request %s failed: %s", url, exc)
            return None

    def _http_text(self, url: str) -> str | None:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github.v3.raw",
                "User-Agent": "oxenclaw-skills",
                **({"Authorization": f"Bearer {self._token}"} if self._token else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.debug("github raw fetch %s failed: %s", url, exc)
            return None

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def _index_tap(self, tap: _TapSpec) -> _TapCache:
        key = tap.display
        cached = self._cache.get(key)
        if cached and (time.time() - cached.fetched_at) < _TREE_CACHE_TTL:
            return cached
        cache = _TapCache(fetched_at=time.time())
        url = (
            f"https://api.github.com/repos/{tap.owner}/{tap.repo}"
            f"/git/trees/{tap.branch}?recursive=1"
        )
        payload = self._http_json(url)
        if not isinstance(payload, dict):
            self._cache[key] = cache
            return cache
        tree = payload.get("tree")
        if not isinstance(tree, list):
            self._cache[key] = cache
            return cache
        prefix = tap.subpath.rstrip("/")
        for node in tree:
            if not isinstance(node, dict):
                continue
            path = node.get("path")
            ntype = node.get("type")
            if ntype != "blob" or not isinstance(path, str):
                continue
            if not path.endswith("SKILL.md"):
                continue
            if prefix and not path.startswith(prefix + "/"):
                continue
            slug_dir = path[: -len("SKILL.md")].rstrip("/")
            slug = slug_dir.split("/")[-1] if slug_dir else "skill"
            ref = SkillRef(
                id=f"{tap.owner}/{tap.repo}#{tap.branch}/{path}",
                slug=slug,
                source_id=self.source_id,
                description="",
                trust_level=self.trust_level,
                tags=(tap.owner,),
            )
            cache.refs.append(ref)
            cache.skill_md_paths[ref.id] = path
        self._cache[key] = cache
        return cache

    # ------------------------------------------------------------------
    # SkillSource API
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[SkillRef]:
        q = (query or "").strip().lower()
        words = q.split()
        out: list[SkillRef] = []
        for tap in self._taps:
            cache = self._index_tap(tap)
            for ref in cache.refs:
                slug = ref.slug.lower()
                if not q or q in slug or any(w in slug for w in words):
                    out.append(ref)
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
        return out[:limit]

    def fetch(self, skill_id: str) -> SkillBundle:
        manifest, body = self._fetch_skill_md(skill_id)
        return SkillBundle(manifest=manifest, body=body, files={})

    def inspect(self, skill_id: str) -> SkillManifest:
        manifest, _ = self._fetch_skill_md(skill_id)
        return manifest

    def _fetch_skill_md(self, skill_id: str) -> tuple[SkillManifest, str]:
        # `skill_id` shape: "owner/repo#branch/path"
        if "#" not in skill_id:
            raise SkillManifestError(f"unsupported github skill id {skill_id!r}")
        repo_part, rest = skill_id.split("#", 1)
        if "/" not in rest:
            raise SkillManifestError(f"missing path in github skill id {skill_id!r}")
        branch, path = rest.split("/", 1)
        owner_repo = repo_part.strip("/")
        url = f"https://raw.githubusercontent.com/{owner_repo}/{branch}/{path}"
        text = self._http_text(url)
        if text is None:
            raise SkillManifestError(f"could not fetch {skill_id!r}")
        return parse_skill_text(text)


__all__ = ["DEFAULT_TAPS", "GitHubSource"]
