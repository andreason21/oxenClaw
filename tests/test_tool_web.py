"""Phase T1: web_fetch + web_search tool tests."""

from __future__ import annotations

import pytest

from oxenclaw.pi.registry import InMemoryAuthStorage
from oxenclaw.tools_pkg.web import (
    SearchHit,
    SSRFBlocked,
    assert_public_url,
    build_default_providers,
    extract_readable_text,
    search_with_fallback,
    web_fetch_tool,
    web_search_tool,
)

# ─── SSRF ────────────────────────────────────────────────────────────


async def test_ssrf_rejects_loopback() -> None:
    with pytest.raises(SSRFBlocked):
        await assert_public_url("http://127.0.0.1/")
    with pytest.raises(SSRFBlocked):
        await assert_public_url("http://[::1]/")


async def test_ssrf_rejects_private_rfc1918() -> None:
    with pytest.raises(SSRFBlocked):
        await assert_public_url("http://10.0.0.1/")
    with pytest.raises(SSRFBlocked):
        await assert_public_url("http://192.168.1.1/")
    with pytest.raises(SSRFBlocked):
        await assert_public_url("http://172.16.0.1/")


async def test_ssrf_rejects_link_local_and_metadata_endpoints() -> None:
    with pytest.raises(SSRFBlocked):
        await assert_public_url("http://169.254.169.254/")  # AWS/GCP metadata
    with pytest.raises(SSRFBlocked):
        await assert_public_url("http://0.0.0.0/")


async def test_ssrf_rejects_non_http_schemes() -> None:
    with pytest.raises(SSRFBlocked):
        await assert_public_url("file:///etc/passwd")
    with pytest.raises(SSRFBlocked):
        await assert_public_url("ftp://example.com/")


async def test_ssrf_accepts_public_ip() -> None:
    # 1.1.1.1 is global; this is a numeric host so DNS isn't consulted.
    await assert_public_url("https://1.1.1.1/")


# ─── readability ─────────────────────────────────────────────────────


def test_readability_strips_script_and_style() -> None:
    html = """
    <html><head><style>body{x:y}</style></head>
    <body>
      <script>alert('x')</script>
      <p>Hello world.</p>
      <div>Block two.</div>
    </body></html>
    """
    out = extract_readable_text(html)
    assert "alert" not in out
    assert "x:y" not in out
    assert "Hello world." in out
    assert "Block two." in out


def test_readability_collapses_whitespace_and_keeps_block_breaks() -> None:
    html = "<p>line one</p><p>line two</p>"
    out = extract_readable_text(html)
    assert "line one" in out
    assert "line two" in out
    assert out.index("line one") < out.index("line two")


# ─── search providers + dispatcher ──────────────────────────────────


class _FakeProvider:
    def __init__(self, name: str, hits: list[SearchHit]) -> None:
        self.name = name
        self._hits = hits
        self.calls = 0

    async def search(self, query: str, *, k: int) -> list[SearchHit]:
        self.calls += 1
        return list(self._hits)


class _FailProvider:
    name = "boom"

    async def search(self, query: str, *, k: int) -> list[SearchHit]:
        raise RuntimeError("intentional")


async def test_dispatcher_returns_first_nonempty_provider() -> None:
    p1 = _FakeProvider("a", [])
    p2 = _FakeProvider("b", [SearchHit(title="hit", url="https://x", snippet="s")])
    p3 = _FakeProvider("c", [SearchHit(title="other", url="https://y", snippet="t")])
    used, hits = await search_with_fallback("q", [p1, p2, p3], k=5)
    assert used == "b"
    assert len(hits) == 1
    assert p1.calls == 1 and p2.calls == 1
    assert p3.calls == 0  # short-circuited after first non-empty


async def test_dispatcher_skips_failing_providers() -> None:
    p1 = _FailProvider()
    p2 = _FakeProvider("ok", [SearchHit(title="t", url="https://x", snippet="s")])
    used, hits = await search_with_fallback("q", [p1, p2], k=5)
    assert used == "ok"
    assert len(hits) == 1


async def test_dispatcher_empty_when_all_fail_or_empty() -> None:
    used, hits = await search_with_fallback("q", [_FakeProvider("e", [])], k=5)
    assert used == "none"
    assert hits == []


async def test_default_providers_skips_unkeyed_and_includes_ddg(monkeypatch) -> None:
    auth = InMemoryAuthStorage({"brave": "k1", "tavily": "k2"})  # type: ignore[dict-item]
    providers = await build_default_providers(auth)
    names = [p.name for p in providers]
    assert "brave" in names and "tavily" in names
    assert "exa" not in names  # no key → skipped
    assert names[-1] == "duckduckgo"  # always-last fallback


# ─── tool factories shape ───────────────────────────────────────────


def test_web_fetch_tool_metadata() -> None:
    t = web_fetch_tool()
    assert t.name == "web_fetch"
    assert "url" in t.input_schema["properties"]


def test_web_search_tool_metadata() -> None:
    t = web_search_tool()
    assert t.name == "web_search"
    assert "query" in t.input_schema["properties"]


async def test_web_search_tool_with_injected_provider_returns_summary() -> None:
    provider = _FakeProvider(
        "inj",
        [
            SearchHit(title="One", url="https://x.com/1", snippet="first"),
            SearchHit(title="Two", url="https://x.com/2", snippet="second"),
        ],
    )
    t = web_search_tool(providers=[provider])
    out = await t.execute({"query": "anything", "k": 3})
    assert "[via inj]" in out
    assert "https://x.com/1" in out
    assert "https://x.com/2" in out


async def test_web_search_zero_hits_returns_recovery_hint() -> None:
    """When every provider in the chain returns zero hits the tool
    must surface recovery suggestions (web_fetch, rephrase, env vars)
    instead of a bare 'no results' string. Mirrors openclaw's
    chaining guide: 0 hits is data, not a stopping signal."""
    empty = _FakeProvider("ddg", [])
    t = web_search_tool(providers=[empty])
    out = await t.execute({"query": "AI semiconductor outlook 2026", "k": 5})
    assert "no results" in out
    assert "web_fetch" in out
    assert "rephras" in out.lower() or "phrasing" in out.lower()
    assert "BRAVE_API_KEY" in out or "TAVILY_API_KEY" in out


async def test_web_search_zero_hits_routes_weather_to_dedicated_tool() -> None:
    """A weather query that DDG can't answer must route the model to
    the `weather` tool — production hit: gemma4 called web_search for
    "날씨 알려줘", got 0 hits from DDG, then gave up entirely. The
    recovery hint now names the right tool so the model retries."""
    empty = _FakeProvider("ddg", [])
    t = web_search_tool(providers=[empty])
    out_ko = await t.execute({"query": "날씨 알려줘", "k": 5})
    out_en = await t.execute({"query": "weather in Seoul", "k": 5})
    for out in (out_ko, out_en):
        assert "weather query detected" in out
        assert "weather(city=" in out
        assert "wrong tool for weather" in out


async def test_web_search_zero_hits_routes_time_query_to_get_time() -> None:
    empty = _FakeProvider("ddg", [])
    t = web_search_tool(providers=[empty])
    out = await t.execute({"query": "what time is it 지금", "k": 5})
    assert "time query detected" in out
    assert "get_time" in out


async def test_web_search_accepts_queries_array_alias() -> None:
    """Production hit: gemma4 emitted `{queries: ['현재 위치의 날씨']}`
    against `web_search`. The pre-validator must lift the array's
    first element onto the `query` field."""
    provider = _FakeProvider("inj", [SearchHit(title="t", url="https://x", snippet="s")])
    t = web_search_tool(providers=[provider])
    out = await t.execute({"queries": ["현재 위치의 날씨"]})
    assert "[via inj]" in out
    assert provider.calls == 1


async def test_web_search_accepts_q_question_text_aliases() -> None:
    """Other common drift forms: `q`, `question`, `text`, `prompt`, `topic`."""
    provider = _FakeProvider("inj", [SearchHit(title="t", url="https://x", snippet="s")])
    t = web_search_tool(providers=[provider])
    for variant in ({"q": "x"}, {"question": "x"}, {"text": "x"}, {"prompt": "x"}, {"topic": "x"}):
        out = await t.execute(variant)
        assert "via inj" in out


async def test_web_search_accepts_limit_and_top_k_for_k() -> None:
    """The validator must accept `limit` / `top_k` aliases for `k`.
    We verify by capturing the k value the underlying provider sees."""
    seen_k: list[int] = []

    class _CapturingProvider:
        name = "cap"

        async def search(self, query: str, *, k: int) -> list[SearchHit]:
            seen_k.append(k)
            return [SearchHit(title="t", url="https://x", snippet="s")]

    t = web_search_tool(providers=[_CapturingProvider()])
    await t.execute({"query": "x", "limit": 3})
    await t.execute({"query": "x", "top_k": 7})
    await t.execute({"query": "x", "max_results": 11})
    assert seen_k == [3, 7, 11]


async def test_web_search_drops_unknown_keys_silently() -> None:
    provider = _FakeProvider("inj", [SearchHit(title="t", url="https://x", snippet="s")])
    t = web_search_tool(providers=[provider])
    out = await t.execute({"query": "x", "scope": "global", "agent_id": "a"})
    assert "via inj" in out


async def test_web_search_redirect_handler_fires_on_zero_hits() -> None:
    """Production hit: gemma4 emits `web_search` for "날씨 알려줘",
    DDG returns 0 hits, model ignores recovery hint and replies
    "Hello!". Fix: registered redirect handler runs the right tool
    inline so the model sees a successful answer."""
    empty = _FakeProvider("ddg", [])
    seen: list[str] = []

    async def weather_redirect(query: str) -> str:
        seen.append(query)
        return "Suwon: 🌦 +18°C"

    handlers = [
        ("weather", lambda q: "날씨" in q or "weather" in q, weather_redirect),
    ]
    t = web_search_tool(providers=[empty], redirect_handlers=handlers)
    out = await t.execute({"query": "수원 날씨"})
    assert "auto-redirected from web_search → weather" in out
    assert "Suwon: 🌦 +18°C" in out
    assert seen == ["수원 날씨"]


async def test_web_search_redirect_handler_skipped_when_predicate_false() -> None:
    """Non-matching topic falls through to the recovery-hint path."""
    empty = _FakeProvider("ddg", [])
    fired = False

    async def never(query: str) -> str:
        nonlocal fired
        fired = True
        return "should not appear"

    handlers = [("weather", lambda q: "날씨" in q, never)]
    t = web_search_tool(providers=[empty], redirect_handlers=handlers)
    out = await t.execute({"query": "rust ownership rules"})
    assert "no results" in out
    assert "auto-redirected" not in out
    assert fired is False


async def test_web_search_redirect_handler_failure_falls_through() -> None:
    """Buggy redirect handler must not crash the tool — fall through
    to the recovery hint."""
    empty = _FakeProvider("ddg", [])

    async def broken(query: str) -> str:
        raise RuntimeError("oops")

    handlers = [("weather", lambda q: True, broken)]
    t = web_search_tool(providers=[empty], redirect_handlers=handlers)
    out = await t.execute({"query": "x"})
    assert "no results" in out
    assert "auto-redirected" not in out


async def test_web_search_redirect_first_handler_wins() -> None:
    """Multiple handlers — first matching one wins, rest don't run."""
    empty = _FakeProvider("ddg", [])
    fired: list[str] = []

    async def first(q: str) -> str:
        fired.append("first")
        return "first answer"

    async def second(q: str) -> str:
        fired.append("second")
        return "second answer"

    handlers = [
        ("first", lambda q: True, first),
        ("second", lambda q: True, second),
    ]
    t = web_search_tool(providers=[empty], redirect_handlers=handlers)
    out = await t.execute({"query": "x"})
    assert "first answer" in out
    assert fired == ["first"]


async def test_web_search_zero_hits_no_topic_match_keeps_generic_hint() -> None:
    """A non-topical query must still get the generic recovery hint
    (no topic-specific line) so the model has fallback guidance."""
    empty = _FakeProvider("ddg", [])
    t = web_search_tool(providers=[empty])
    out = await t.execute({"query": "completely off-topic random thing", "k": 5})
    assert "no results" in out
    assert "weather query detected" not in out
    assert "time query detected" not in out
    assert "github query detected" not in out
    assert "rephras" in out.lower() or "phrasing" in out.lower()


def test_build_default_search_chain_picks_up_env_keys(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Setting BRAVE/TAVILY/EXA env vars adds the corresponding
    providers to the chain, with DuckDuckGo always last as the
    no-credential fallback."""
    from oxenclaw.tools_pkg.web import build_default_search_chain

    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    chain_just_ddg = build_default_search_chain()
    assert [p.name for p in chain_just_ddg] == ["duckduckgo"]

    monkeypatch.setenv("BRAVE_API_KEY", "k1")
    monkeypatch.setenv("TAVILY_API_KEY", "k2")
    chain = build_default_search_chain()
    names = [p.name for p in chain]
    # Brave first (preference order), DDG always last.
    assert names[0] == "brave"
    assert "tavily" in names
    assert names[-1] == "duckduckgo"


async def test_web_fetch_tool_blocks_ssrf_target() -> None:
    t = web_fetch_tool()
    out = await t.execute({"url": "http://10.0.0.1/", "max_bytes": 1000, "readability": False})
    assert "web_fetch error" in out
    assert "non-public" in out or "private" in out.lower()
