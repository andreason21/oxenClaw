"""web_fetch + web_search tools with SSRF guard + provider fallback.

The SSRF guard + DNS pinning + audit are now provided by
`oxenclaw.security.net` (see `guarded_session`). This module wires the
shared net layer into the LLM-callable tools.

- `web_guarded_fetch` — single-source HTTP GET via `guarded_session`,
  with size cap, timeout, per-hop re-validation, optional readability.
- `WebSearchProvider` Protocol + concrete impls for **brave**,
  **duckduckgo**, **searxng**, **tavily**, **exa**.
- `web_fetch_tool(...)` and `web_search_tool(...)` factory wrappers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

import aiohttp
from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.pi.registry import AuthStorage
from oxenclaw.plugin_sdk.runtime_env import get_logger
from oxenclaw.security.net import NetPolicy
from oxenclaw.security.net.guarded_fetch import (
    guarded_session,
    policy_pre_flight,
)
from oxenclaw.security.net.ssrf import SsrFBlockedError

logger = get_logger("tools.web")

DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_USER_AGENT = "oxenclaw-web/1.0 (+https://github.com/oxenclaw)"


# ─── Backwards-compatible aliases ────────────────────────────────────


# Existing callers + tests import `SSRFBlocked` and `assert_public_url`.
# Keep both names working by delegating to the new net layer.
SSRFBlocked = SsrFBlockedError


async def assert_public_url(url: str) -> None:
    """Refuse non-public URLs. Backwards-compat shim around the new net
    layer using the strict default `NetPolicy`."""
    policy_pre_flight(url, NetPolicy())


# ─── Readability (very small HTML→text) ──────────────────────────────


class _TextExtractor(HTMLParser):
    """Lightweight HTML→text extractor: drops script/style/nav tags,
    preserves block boundaries with newlines."""

    _BLOCK = {
        "p",
        "div",
        "br",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "header",
        "footer",
        "main",
    }
    _SKIP = {"script", "style", "noscript", "nav", "svg", "header", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:  # type: ignore[type-arg]
        if tag.lower() in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        if t in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._parts.append(text + " ")

    def text(self) -> str:
        joined = "".join(self._parts)
        # Collapse runs of whitespace inside lines, preserve newlines.
        lines = [re.sub(r"\s+", " ", ln).strip() for ln in joined.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def extract_readable_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


# ─── Guarded fetch ───────────────────────────────────────────────────


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status: int
    content_type: str | None
    body: str
    truncated: bool


async def web_guarded_fetch(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    readability: bool = True,
    user_agent: str = DEFAULT_USER_AGENT,
    session: aiohttp.ClientSession | None = None,
    policy: NetPolicy | None = None,
) -> FetchResult:
    """SSRF-checked GET that re-validates every redirect hop.

    When `session` is omitted, opens a `guarded_session(policy)` so the
    DNS-pinning resolver + audit hooks apply. When `session` is supplied,
    the caller is responsible for using a guarded session.
    """
    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    pol = policy or NetPolicy()

    async def _do(s: aiohttp.ClientSession, current: str) -> FetchResult:
        # Manually follow redirects so each hop is policy-checked.
        for _ in range(5):  # max 5 hops
            policy_pre_flight(current, pol)
            async with s.get(
                current, headers=headers, allow_redirects=False, timeout=timeout
            ) as resp:
                if 300 <= resp.status < 400 and "Location" in resp.headers:
                    nxt = resp.headers["Location"]
                    if nxt.startswith("/"):
                        parsed = urlparse(current)
                        nxt = f"{parsed.scheme}://{parsed.netloc}{nxt}"
                    current = nxt
                    continue
                ctype = resp.headers.get("Content-Type")
                raw = await resp.content.read(max_bytes + 1)
                truncated = len(raw) > max_bytes
                if truncated:
                    raw = raw[:max_bytes]
                text = raw.decode("utf-8", errors="replace")
                if readability and ctype and "html" in ctype.lower():
                    text = extract_readable_text(text)
                return FetchResult(
                    url=url,
                    final_url=current,
                    status=resp.status,
                    content_type=ctype,
                    body=text,
                    truncated=truncated,
                )
        raise RuntimeError(f"too many redirects fetching {url!r}")

    if session is not None:
        return await _do(session, url)
    async with guarded_session(pol, timeout_total=timeout_seconds) as s:
        return await _do(s, url)


# ─── Search providers ────────────────────────────────────────────────


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str


class WebSearchProvider:
    """Protocol-shape base; subclasses override `search`."""

    name: str

    async def search(self, query: str, *, k: int) -> list[SearchHit]:
        raise NotImplementedError


@dataclass
class BraveSearch(WebSearchProvider):
    api_key: str
    name: str = "brave"

    async def search(self, query: str, *, k: int) -> list[SearchHit]:
        url = "https://api.search.brave.com/res/v1/web/search"
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
        }
        params = {"q": query, "count": min(20, max(1, k))}
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status >= 400:
                    return []
                data = await resp.json()
        results = (data.get("web") or {}).get("results") or []
        return [
            SearchHit(
                title=r.get("title") or "",
                url=r.get("url") or "",
                snippet=r.get("description") or "",
            )
            for r in results[:k]
        ]


@dataclass
class TavilySearch(WebSearchProvider):
    api_key: str
    name: str = "tavily"

    async def search(self, query: str, *, k: int) -> list[SearchHit]:
        url = "https://api.tavily.com/search"
        async with (
            aiohttp.ClientSession() as s,
            s.post(
                url,
                json={"api_key": self.api_key, "query": query, "max_results": k},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp,
        ):
            if resp.status >= 400:
                return []
            data = await resp.json()
        return [
            SearchHit(
                title=r.get("title") or "",
                url=r.get("url") or "",
                snippet=r.get("content") or "",
            )
            for r in (data.get("results") or [])[:k]
        ]


@dataclass
class ExaSearch(WebSearchProvider):
    api_key: str
    name: str = "exa"

    async def search(self, query: str, *, k: int) -> list[SearchHit]:
        url = "https://api.exa.ai/search"
        async with (
            aiohttp.ClientSession() as s,
            s.post(
                url,
                headers={"x-api-key": self.api_key},
                json={"query": query, "numResults": k},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp,
        ):
            if resp.status >= 400:
                return []
            data = await resp.json()
        return [
            SearchHit(
                title=r.get("title") or "",
                url=r.get("url") or "",
                snippet=r.get("text") or "",
            )
            for r in (data.get("results") or [])[:k]
        ]


@dataclass
class DuckDuckGoSearch(WebSearchProvider):
    """No-key DDG instant-answer endpoint. Returns RelatedTopics."""

    name: str = "duckduckgo"

    async def search(self, query: str, *, k: int) -> list[SearchHit]:
        url = "https://api.duckduckgo.com/"
        params = {"q": query, "format": "json", "no_redirect": "1", "no_html": "1"}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status >= 400:
                    return []
                data = await resp.json(content_type=None)
        out: list[SearchHit] = []
        for r in (data.get("RelatedTopics") or [])[:k]:
            if "Text" in r and "FirstURL" in r:
                out.append(SearchHit(title=r["Text"][:120], url=r["FirstURL"], snippet=r["Text"]))
        return out


@dataclass
class SearXNGSearch(WebSearchProvider):
    base_url: str  # e.g. https://searx.example.com
    name: str = "searxng"

    async def search(self, query: str, *, k: int) -> list[SearchHit]:
        url = self.base_url.rstrip("/") + "/search"
        params = {"q": query, "format": "json"}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status >= 400:
                    return []
                data = await resp.json(content_type=None)
        return [
            SearchHit(
                title=r.get("title") or "",
                url=r.get("url") or "",
                snippet=r.get("content") or "",
            )
            for r in (data.get("results") or [])[:k]
        ]


# ─── Search dispatcher with provider fallback ────────────────────────


def build_default_search_chain(*, searxng_url: str | None = None) -> list[WebSearchProvider]:
    """Build a search-provider chain from environment variables alone.

    Tries `BRAVE_API_KEY`, `TAVILY_API_KEY`, `EXA_API_KEY`, optional
    `searxng_url`, then DuckDuckGo as the no-credential fallback.
    Used by `web_search_tool()` when the caller hasn't injected
    providers (the gateway-built chain via `build_default_providers`
    is preferred when AuthStorage is available; this is the
    last-resort when constructing the tool standalone).
    """
    import os

    providers: list[WebSearchProvider] = []
    if k := os.environ.get("BRAVE_API_KEY"):
        providers.append(BraveSearch(api_key=k))
    if k := os.environ.get("TAVILY_API_KEY"):
        providers.append(TavilySearch(api_key=k))
    if k := os.environ.get("EXA_API_KEY"):
        providers.append(ExaSearch(api_key=k))
    url = searxng_url or os.environ.get("SEARXNG_URL")
    if url:
        providers.append(SearXNGSearch(base_url=url))
    providers.append(DuckDuckGoSearch())
    return providers


async def build_default_providers(
    auth: AuthStorage, *, searxng_url: str | None = None
) -> list[WebSearchProvider]:
    """Build providers based on what credentials are available.

    Provider preference order: brave → tavily → exa → searxng → duckduckgo.
    Missing-keyed providers are skipped silently; DDG is always last as a
    no-credential fallback so search never hard-fails.
    """
    providers: list[WebSearchProvider] = []
    # Reuse the AuthStorage abstraction even though "brave" etc aren't
    # ProviderId values — it's a convenient generic key/value secrets bag.
    for key, ctor in (
        ("brave", lambda k: BraveSearch(api_key=k)),
        ("tavily", lambda k: TavilySearch(api_key=k)),
        ("exa", lambda k: ExaSearch(api_key=k)),
    ):
        api_key = await auth.get(key)  # type: ignore[arg-type]
        if api_key:
            providers.append(ctor(api_key))
    if searxng_url:
        providers.append(SearXNGSearch(base_url=searxng_url))
    providers.append(DuckDuckGoSearch())
    return providers


async def search_with_fallback(
    query: str, providers: Iterable[WebSearchProvider], *, k: int = 5
) -> tuple[str, list[SearchHit]]:
    """Try each provider in order; return the first non-empty result set
    along with the provider name that succeeded."""
    for p in providers:
        try:
            hits = await p.search(query, k=k)
        except Exception:
            logger.exception("provider %s raised", p.name)
            continue
        if hits:
            return p.name, hits
    return "none", []


# ─── Tool factories ──────────────────────────────────────────────────


class _FetchArgs(BaseModel):
    url: str = Field(..., description="HTTP(S) URL to fetch.")
    readability: bool = Field(True, description="Strip HTML to readable text when content is HTML.")
    max_bytes: int = Field(DEFAULT_MAX_BYTES, description="Hard cap on response bytes read.", gt=0)


class _SearchArgs(BaseModel):
    query: str = Field(..., description="Search query string.")
    k: int = Field(5, description="Max number of results to return.", gt=0, le=20)


def web_fetch_tool() -> Tool:
    async def _h(args: _FetchArgs) -> str:
        try:
            res = await web_guarded_fetch(
                args.url,
                max_bytes=args.max_bytes,
                readability=args.readability,
            )
        except SSRFBlocked as exc:
            return f"web_fetch error: {exc}"
        except (TimeoutError, aiohttp.ClientError) as exc:
            return f"web_fetch network error: {exc}"
        body_preview = res.body[:8000]
        suffix = "\n[...truncated]" if res.truncated or len(res.body) > 8000 else ""
        return (
            f"GET {res.final_url} → {res.status} ({res.content_type or '?'})\n"
            f"{body_preview}{suffix}"
        )

    return FunctionTool(
        name="web_fetch",
        description=(
            "Fetch a public URL over HTTP(S). Refuses private/non-public addresses "
            "(SSRF guard). Returns readable text for HTML, raw bytes-as-text otherwise."
        ),
        input_model=_FetchArgs,
        handler=_h,
    )


def web_search_tool(*, providers: list[WebSearchProvider] | None = None) -> Tool:
    """Build a `web_search` tool. Pass `providers` to inject; otherwise
    the tool falls back to DuckDuckGo (no-key) at call time."""

    async def _h(args: _SearchArgs) -> str:
        chain = providers or build_default_search_chain()
        used, hits = await search_with_fallback(args.query, chain, k=args.k)
        if not hits:
            # Help the LLM recover instead of giving up. Mirrors the openclaw
            # chaining guide: "0 hits is data, try web_fetch on a known URL
            # or rephrase the query."
            return (
                f"no results (tried providers: {[p.name for p in chain]}).\n"
                "Recovery suggestions for the model:\n"
                "  - try a different phrasing (translate Korean ↔ English, "
                "drop adjectives, add `site:` for a known authority).\n"
                "  - call `web_fetch` directly on a likely URL "
                "(e.g. an industry blog, official report) and read the body.\n"
                "  - if multiple search backends are available "
                "(BRAVE_API_KEY / TAVILY_API_KEY / EXA_API_KEY env vars), "
                "set them in the gateway environment and reload."
            )
        lines = [f"[via {used}]"]
        for i, h in enumerate(hits, start=1):
            lines.append(f"{i}. {h.title}")
            lines.append(f"   {h.url}")
            if h.snippet:
                lines.append(f"   {h.snippet[:300]}")
        return "\n".join(lines)

    return FunctionTool(
        name="web_search",
        description=(
            "Search the web. Tries providers in order (Brave → Tavily → Exa → "
            "SearXNG → DuckDuckGo) and returns the first non-empty result set."
        ),
        input_model=_SearchArgs,
        handler=_h,
    )


__all__ = [
    "BraveSearch",
    "DuckDuckGoSearch",
    "ExaSearch",
    "FetchResult",
    "SSRFBlocked",
    "SearXNGSearch",
    "SearchHit",
    "TavilySearch",
    "WebSearchProvider",
    "assert_public_url",
    "build_default_providers",
    "build_default_search_chain",
    "extract_readable_text",
    "search_with_fallback",
    "web_fetch_tool",
    "web_guarded_fetch",
    "web_search_tool",
]
