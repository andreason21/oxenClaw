"""AuthKeyPool: round-robin + cooldown failover."""

from __future__ import annotations

from oxenclaw.pi.auth_pool import AuthKeyPool
from oxenclaw.pi.registry import InMemoryAuthStorage, PoolBackedAuthStorage


async def test_round_robin_advances_cursor() -> None:
    pool = AuthKeyPool()
    pool.add("openai", "k1", "sk-1")
    pool.add("openai", "k2", "sk-2")
    pool.add("openai", "k3", "sk-3")
    seen = []
    for _ in range(6):
        out = await pool.get("openai")
        assert out is not None
        seen.append(out[0])
    assert seen == ["k1", "k2", "k3", "k1", "k2", "k3"]


async def test_cooldown_skips_failed_keys() -> None:
    pool = AuthKeyPool(cooldown_seconds=10.0)
    pool.add("openai", "good", "sk-good")
    pool.add("openai", "bad", "sk-bad")
    # First call returns "good", second "bad" — then bad fails.
    await pool.get("openai")  # consumes "good"
    out2 = await pool.get("openai")
    assert out2 is not None and out2[0] == "bad"
    await pool.report_failure("openai", "bad", status=401)
    # Now bad is in cooldown — every subsequent get returns "good".
    for _ in range(3):
        out = await pool.get("openai")
        assert out is not None and out[0] == "good"


async def test_locked_key_pinned_even_if_cooling() -> None:
    pool = AuthKeyPool(cooldown_seconds=10.0)
    pool.add("anthropic", "primary", "sk-p")
    pool.add("anthropic", "backup", "sk-b")
    pool.lock("anthropic", "primary")
    # Mark primary as failing — but the lock pins it anyway.
    await pool.report_failure("anthropic", "primary", status=429)
    out = await pool.get("anthropic")
    assert out is not None and out[0] == "primary"


async def test_report_failure_coalesces_burst() -> None:
    """Repeated failures within 5s shouldn't multiply the cooldown."""
    pool = AuthKeyPool(cooldown_seconds=5.0)
    pool.add("openai", "k", "sk")
    await pool.report_failure("openai", "k", status=500)
    first_cd = pool.by_provider["openai"][0].cooldown_until
    await pool.report_failure("openai", "k", status=500)
    second_cd = pool.by_provider["openai"][0].cooldown_until
    # Same cooldown, no escalation.
    assert first_cd == second_cd


async def test_report_success_resets_streak() -> None:
    pool = AuthKeyPool()
    pool.add("openai", "k", "sk")
    await pool.report_failure("openai", "k", status=429)
    assert pool.by_provider["openai"][0].failure_count == 1
    await pool.report_success("openai", "k")
    assert pool.by_provider["openai"][0].failure_count == 0
    assert pool.by_provider["openai"][0].cooldown_until == 0.0


async def test_pool_backed_auth_falls_back_to_inner() -> None:
    """When the pool has no entries for a provider, get() must fall
    through to the wrapped AuthStorage (env / sqlite)."""
    pool = AuthKeyPool()
    inner = InMemoryAuthStorage({"openai": "sk-from-inner"})  # type: ignore[dict-item]
    storage = PoolBackedAuthStorage(pool, inner)
    out = await storage.get("openai")  # type: ignore[arg-type]
    assert out == "sk-from-inner"


async def test_pool_backed_auth_uses_pool_when_present() -> None:
    pool = AuthKeyPool()
    pool.add("openai", "primary", "sk-from-pool")
    inner = InMemoryAuthStorage({"openai": "sk-from-inner"})  # type: ignore[dict-item]
    storage = PoolBackedAuthStorage(pool, inner)
    out = await storage.get("openai")  # type: ignore[arg-type]
    assert out == "sk-from-pool"
    assert storage.last_key_id_for("openai") == "primary"  # type: ignore[arg-type]


async def test_keys_for_inspector_lists_state() -> None:
    pool = AuthKeyPool()
    pool.add("openai", "k1", "sk1")
    pool.add("openai", "k2", "sk2")
    await pool.report_failure("openai", "k1", status=429)
    rows = pool.keys_for("openai")
    by_id = {r["key_id"]: r for r in rows}
    assert by_id["k1"]["alive"] is False
    assert by_id["k2"]["alive"] is True
    assert by_id["k1"]["failure_count"] == 1
