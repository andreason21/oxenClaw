"""Lockfile + OriginMetadata tests."""

from __future__ import annotations

from sampyclaw.clawhub.lockfile import Lockfile, OriginMetadata


def test_lockfile_empty_when_missing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    lf = Lockfile.load(tmp_path / "lock.json")
    assert lf.skills == {}


def test_lockfile_upsert_save_load(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "nested" / "lock.json"
    lf = Lockfile()
    lf.upsert("foo", "1.0.0", installed_at=100.0)
    lf.upsert("bar", "0.1.0", installed_at=200.0)
    lf.save(path)

    reloaded = Lockfile.load(path)
    assert set(reloaded.skills) == {"foo", "bar"}
    assert reloaded.skills["foo"].version == "1.0.0"
    assert reloaded.skills["bar"].installed_at == 200.0


def test_lockfile_remove(tmp_path) -> None:  # type: ignore[no-untyped-def]
    lf = Lockfile()
    lf.upsert("x", "1.0.0")
    assert lf.remove("x") is True
    assert lf.remove("x") is False


def test_origin_round_trip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "origin.json"
    om = OriginMetadata(
        registry="https://clawhub.ai",
        slug="foo",
        installed_version="1.2.3",
        installed_at=42.0,
        integrity="sha256-deadbeef",
    )
    om.save(path)
    reloaded = OriginMetadata.load(path)
    assert reloaded is not None
    assert reloaded.slug == "foo"
    assert reloaded.installed_version == "1.2.3"
    assert reloaded.integrity == "sha256-deadbeef"


def test_origin_load_missing_returns_none(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert OriginMetadata.load(tmp_path / "nope.json") is None


def test_lockfile_load_garbage_returns_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "lock.json"
    path.write_text("not json {}{")
    assert Lockfile.load(path).skills == {}
