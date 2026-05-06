"""skills.* gateway RPC tests with a mocked ClawHubClient + fixture archive."""

from __future__ import annotations

import io
import zipfile
from unittest.mock import AsyncMock

import pytest

from oxenclaw.clawhub.client import ClawHubClient, sha256_integrity
from oxenclaw.clawhub.installer import SkillInstaller
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.gateway.router import Router
from oxenclaw.gateway.skills_methods import register_skills_methods

SAMPLE_SKILL_MD = """---
name: foo
description: A test skill.
metadata:
  openclaw:
    emoji: 🧪
    requires:
      bins: [foo]
---

# body
"""


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("foo/SKILL.md", SAMPLE_SKILL_MD)
    return buf.getvalue()


@pytest.fixture()
def setup(tmp_path):  # type: ignore[no-untyped-def]
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()

    client = ClawHubClient()
    archive = _zip_bytes()
    client.search_skills = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"slug": "foo", "displayName": "Foo"}]
    )
    client.list_skills = AsyncMock(  # type: ignore[method-assign]
        return_value={"results": [{"slug": "foo"}, {"slug": "bar"}]}
    )
    client.fetch_skill_detail = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "skill": {"slug": "foo"},
            "latestVersion": {"version": "1.0.0"},
        }
    )
    client.download_skill_archive = AsyncMock(  # type: ignore[method-assign]
        return_value=(archive, sha256_integrity(archive))
    )
    client.aclose = AsyncMock()  # type: ignore[method-assign]

    installer = SkillInstaller(client, paths=paths)
    router = Router()
    register_skills_methods(router, client=client, installer=installer, paths=paths)
    return router, paths, client, installer


async def test_search_returns_results(setup) -> None:  # type: ignore[no-untyped-def]
    router, _, _, _ = setup
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "skills.search", "params": {"query": "foo"}}
    )
    assert resp.error is None
    assert resp.result["ok"] is True
    assert resp.result["results"][0]["slug"] == "foo"


async def test_list_remote(setup) -> None:  # type: ignore[no-untyped-def]
    router, _, _, _ = setup
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "skills.list_remote", "params": {}}
    )
    assert resp.result["ok"] is True
    assert any(s["slug"] == "bar" for s in resp.result["results"])


async def test_detail(setup) -> None:  # type: ignore[no-untyped-def]
    router, _, _, _ = setup
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "skills.detail", "params": {"slug": "foo"}}
    )
    assert resp.result["ok"] is True
    assert resp.result["detail"]["latestVersion"]["version"] == "1.0.0"


async def test_install_then_list_installed_then_uninstall(setup) -> None:  # type: ignore[no-untyped-def]
    router, paths, _, _ = setup

    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "skills.install", "params": {"slug": "foo"}}
    )
    assert resp.result["ok"] is True
    assert resp.result["version"] == "1.0.0"
    assert resp.result["manifest"]["name"] == "foo"
    assert resp.result["manifest"]["requires"]["bins"] == ["foo"]
    # Even with `with_bins` not set we surface the (empty) plan so
    # dashboards can show "no extra installs needed".
    assert resp.result["bin_install_plan"] == []
    assert resp.result["bin_install"] is None
    assert (paths.home / "skills" / "foo" / "SKILL.md").exists()

    listed = await router.dispatch({"jsonrpc": "2.0", "id": 2, "method": "skills.list_installed"})
    assert listed.result["ok"] is True
    assert listed.result["skills"][0]["slug"] == "foo"
    assert listed.result["skills"][0]["version"] == "1.0.0"

    removed = await router.dispatch(
        {"jsonrpc": "2.0", "id": 3, "method": "skills.uninstall", "params": {"slug": "foo"}}
    )
    assert removed.result == {"ok": True, "removed": True}

    listed2 = await router.dispatch({"jsonrpc": "2.0", "id": 4, "method": "skills.list_installed"})
    assert listed2.result["skills"] == []


async def test_install_existing_without_force_returns_error(setup) -> None:  # type: ignore[no-untyped-def]
    router, _, _, _ = setup
    await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "skills.install", "params": {"slug": "foo"}}
    )
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "skills.install", "params": {"slug": "foo"}}
    )
    assert resp.result["ok"] is False
    assert "already installed" in resp.result["error"]


async def test_update_after_install(setup) -> None:  # type: ignore[no-untyped-def]
    router, _, _, _ = setup
    await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "skills.install", "params": {"slug": "foo"}}
    )
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "skills.update", "params": {"slug": "foo"}}
    )
    assert resp.result["ok"] is True
    assert resp.result["version"] == "1.0.0"


async def test_install_unknown_slug_format(setup) -> None:  # type: ignore[no-untyped-def]
    router, _, _, _ = setup
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "skills.install",
            "params": {"slug": "bad slug!"},
        }
    )
    assert resp.result["ok"] is False


# ─── with_bins flow ─────────────────────────────────────────────────


_YAHOO_SKILL_MD = """---
name: yahoo-finance
description: stock prices.
metadata:
  openclaw:
    requires:
      bins: [jq, yf]
    install:
      - {id: jq, kind: brew, formula: jq, label: "Install jq"}
      - {id: yf, kind: node, package: yahoo-finance2, label: "Install yf"}
      - {id: link, kind: exec, command: "ln -sf a b", label: "Link yf"}
---

# body
"""


def _zip_yahoo_bytes() -> bytes:
    """Same shape as the existing fixture but with the realistic
    yahoo-finance-cli manifest so the bin-install plan has content."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("yahoo-finance-cli/SKILL.md", _YAHOO_SKILL_MD)
    return buf.getvalue()


@pytest.fixture()
def setup_yahoo(tmp_path):  # type: ignore[no-untyped-def]
    """Variant of the base fixture wired to install the yahoo-finance-cli
    skill so the with_bins branch has a non-trivial plan to operate on."""
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    client = ClawHubClient()
    archive = _zip_yahoo_bytes()
    client.fetch_skill_detail = AsyncMock(  # type: ignore[method-assign]
        return_value={"latestVersion": {"version": "1.0.0"}}
    )
    client.download_skill_archive = AsyncMock(  # type: ignore[method-assign]
        return_value=(archive, sha256_integrity(archive))
    )
    client.aclose = AsyncMock()  # type: ignore[method-assign]
    installer = SkillInstaller(client, paths=paths)
    router = Router()
    register_skills_methods(router, client=client, installer=installer, paths=paths)
    return router, paths


async def test_install_returns_plan_even_when_with_bins_false(setup_yahoo) -> None:  # type: ignore[no-untyped-def]
    """Dashboards always want to know `what else would this install?`
    so the plan is included in every install response — not gated on
    the with_bins flag."""
    router, _ = setup_yahoo
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "skills.install",
            "params": {"slug": "yahoo-finance-cli"},
        }
    )
    assert resp.result["ok"] is True
    plan = resp.result["bin_install_plan"]
    assert len(plan) == 3
    assert plan[0]["label"] == "Install jq"
    assert plan[1]["label"] == "Install yf"
    assert plan[2]["decision"] == "skip"  # exec → refused
    assert resp.result["bin_install"] is None  # no execution requested


async def test_install_with_bins_without_opt_in_returns_decline(
    setup_yahoo, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """`with_bins=True` without the env opt-in must NOT execute. The
    response carries an actionable reason so the dashboard can show
    the operator how to enable it."""
    monkeypatch.delenv("OXENCLAW_GATEWAY_BIN_AUTO_INSTALL", raising=False)

    def _no_run(argv):  # type: ignore[no-untyped-def]
        raise AssertionError("must not run without operator opt-in")

    monkeypatch.setattr("oxenclaw.clawhub.bin_installer._default_runner", _no_run)
    router, _ = setup_yahoo
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "skills.install",
            "params": {"slug": "yahoo-finance-cli", "with_bins": True},
        }
    )
    assert resp.result["ok"] is True
    block = resp.result["bin_install"]
    assert block["executed"] is False
    assert "OXENCLAW_GATEWAY_BIN_AUTO_INSTALL" in block["reason"]
    # The plan is still returned so the dashboard can preview it.
    assert len(resp.result["bin_install_plan"]) == 3


async def test_install_with_bins_executes_when_opted_in(
    setup_yahoo, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """When the operator has opted in via env flag, with_bins=True
    auto-runs every runnable step. Refused-kind steps (exec) skip
    cleanly and the run is reported success."""
    import subprocess

    monkeypatch.setenv("OXENCLAW_GATEWAY_BIN_AUTO_INSTALL", "1")
    monkeypatch.setattr(
        "oxenclaw.clawhub.bin_installer.platform.system", lambda: "Linux"
    )
    monkeypatch.setattr(
        "oxenclaw.clawhub.bin_installer._on_path", lambda _n: False
    )
    calls: list[tuple[str, ...]] = []

    def _runner(argv):  # type: ignore[no-untyped-def]
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(
            args=list(argv), returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr("oxenclaw.clawhub.bin_installer._default_runner", _runner)
    router, _ = setup_yahoo
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "skills.install",
            "params": {"slug": "yahoo-finance-cli", "with_bins": True},
        }
    )
    assert resp.result["ok"] is True
    block = resp.result["bin_install"]
    assert block["executed"] is True
    assert block["ok"] is True
    # Two runnable steps fired (apt-fallback for jq, npm for yahoo-finance2);
    # exec step is skipped per refused-kind policy.
    assert calls == [
        ("apt-get", "install", "-y", "jq"),
        ("npm", "install", "-g", "yahoo-finance2"),
    ]
    # Per-step results surface for any client that wants to render them.
    assert len(block["results"]) == 3
    executed = [r for r in block["results"] if r["executed"]]
    assert len(executed) == 2
    assert all(r["exit_code"] == 0 for r in executed)


async def test_install_with_bins_propagates_step_failure(
    setup_yahoo, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """A failing runnable step flips block.ok=False; the install RPC
    itself stays ok=True because the skill files DID install. This
    keeps the contract honest: 'skill installed, but bin step X
    failed' is a different signal from 'install failed entirely'."""
    import subprocess

    monkeypatch.setenv("OXENCLAW_GATEWAY_BIN_AUTO_INSTALL", "1")
    monkeypatch.setattr(
        "oxenclaw.clawhub.bin_installer.platform.system", lambda: "Linux"
    )
    monkeypatch.setattr(
        "oxenclaw.clawhub.bin_installer._on_path", lambda _n: False
    )

    def _runner(argv):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            args=list(argv), returncode=1, stdout="", stderr="boom\n"
        )

    monkeypatch.setattr("oxenclaw.clawhub.bin_installer._default_runner", _runner)
    router, _ = setup_yahoo
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "skills.install",
            "params": {"slug": "yahoo-finance-cli", "with_bins": True},
        }
    )
    assert resp.result["ok"] is True  # the SKILL install part succeeded
    block = resp.result["bin_install"]
    assert block["executed"] is True
    assert block["ok"] is False  # but at least one step failed
