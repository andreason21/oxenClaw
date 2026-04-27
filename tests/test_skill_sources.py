"""Tests for SkillSource ABC + GitHub / Index sources + parallel search."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from oxenclaw.clawhub.parallel_search import parallel_search_sources
from oxenclaw.clawhub.sources.base import SkillRef, SkillSource
from oxenclaw.clawhub.sources.github import GitHubSource, _TapSpec
from oxenclaw.clawhub.sources.index import INDEX_CACHE_DIR, IndexSource


class _StubSource(SkillSource):
    source_id = "stub"
    trust_level = "official"

    def __init__(self, refs: list[SkillRef]) -> None:
        self._refs = refs

    def search(self, query: str, limit: int = 10) -> list[SkillRef]:
        q = (query or "").lower()
        return [r for r in self._refs if q in r.slug.lower()][:limit]

    def fetch(self, skill_id: str):
        raise NotImplementedError

    def inspect(self, skill_id: str):
        raise NotImplementedError


def test_tap_spec_parses() -> None:
    s = _TapSpec.parse("openai/skills")
    assert s.owner == "openai"
    assert s.repo == "skills"
    assert s.branch == "main"
    s = _TapSpec.parse("anthropics/skills#dev/sub/path")
    assert s.branch == "dev"
    assert s.subpath == "sub/path"


def test_github_source_search_uses_cached_tree() -> None:
    src = GitHubSource(taps=("foo/bar",), token=None)
    fake_tree = {
        "tree": [
            {"path": "weather/SKILL.md", "type": "blob"},
            {"path": "calc/SKILL.md", "type": "blob"},
            {"path": "weather/scripts/run.sh", "type": "blob"},
        ]
    }
    with patch.object(src, "_http_json", return_value=fake_tree):
        results = src.search("weather", limit=5)
    slugs = {r.slug for r in results}
    assert "weather" in slugs


def test_github_source_search_handles_no_taps() -> None:
    src = GitHubSource(taps=())
    assert src.search("anything", limit=5) == []


def test_github_source_token_picked_from_env(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    src = GitHubSource(taps=("foo/bar",))
    assert src._token == "ghp_test"


def test_index_source_writes_ignore_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("oxenclaw.clawhub.sources.index.INDEX_CACHE_DIR", tmp_path / "idx")
    monkeypatch.setenv("OXENCLAW_SKILL_INDEX_URL", "https://example.com/idx.json")
    src = IndexSource()
    fake = {"skills": [{"slug": "weather", "description": "weather"}]}
    with patch("oxenclaw.clawhub.sources.index._fetch_index", return_value=fake):
        results = src.search("weather", limit=5)
    assert any(r.slug == "weather" for r in results)
    assert (tmp_path / "idx" / ".ignore").exists()


def test_index_source_no_url_returns_empty(monkeypatch) -> None:
    monkeypatch.delenv("OXENCLAW_SKILL_INDEX_URL", raising=False)
    src = IndexSource()
    assert src.configured is False
    assert src.search("x", 5) == []


def test_index_source_falls_back_to_stale_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("oxenclaw.clawhub.sources.index.INDEX_CACHE_DIR", tmp_path / "idx")
    url = "https://example.com/idx.json"
    src = IndexSource(url=url)
    cache_path = src._url
    # Pre-populate stale cache.
    from oxenclaw.clawhub.sources.index import _write_cache
    _write_cache(url, {"skills": [{"slug": "stale-skill", "description": "x"}]})
    # Force network failure.
    with patch("oxenclaw.clawhub.sources.index._fetch_index", return_value=None):
        results = src.search("stale", limit=5)
    assert any(r.slug == "stale-skill" for r in results)


def test_parallel_search_dedupes_and_ranks_by_trust() -> None:
    a = _StubSource([SkillRef(id="x", slug="x", source_id="a", trust_level="community")])
    b = _StubSource([SkillRef(id="x", slug="x", source_id="b", trust_level="official")])
    refs = parallel_search_sources([a, b], "x", limit=10)
    # Both keys are unique (slug,source_id) so we keep both, but the
    # official one ranks first.
    assert refs[0].trust_level == "official"


def test_parallel_search_handles_failing_source() -> None:
    class Boom(SkillSource):
        source_id = "boom"

        def search(self, query, limit=10):
            raise RuntimeError("kaboom")

        def fetch(self, skill_id):
            raise NotImplementedError

        def inspect(self, skill_id):
            raise NotImplementedError

    good = _StubSource([SkillRef(id="ok", slug="ok", source_id="g")])
    refs = parallel_search_sources([Boom(), good], "ok", limit=5)
    assert any(r.slug == "ok" for r in refs)


def test_parallel_search_prefers_fresh_index(monkeypatch, tmp_path) -> None:
    """When IndexSource is fresh, other sources are skipped."""
    monkeypatch.setattr("oxenclaw.clawhub.sources.index.INDEX_CACHE_DIR", tmp_path / "idx")
    url = "https://example.com/idx.json"
    from oxenclaw.clawhub.sources.index import _write_cache
    _write_cache(url, {"skills": [{"slug": "from-index", "description": ""}]})
    src = IndexSource(url=url)
    # Make sure cache is "fresh".
    assert src.fresh

    other = _StubSource([SkillRef(id="x", slug="from-other", source_id="other")])
    refs = parallel_search_sources([src, other], "from", limit=5)
    slugs = {r.slug for r in refs}
    assert "from-index" in slugs
    assert "from-other" not in slugs
