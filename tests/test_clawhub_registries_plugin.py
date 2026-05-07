"""Plugin-kind registry: config validation + MultiRegistryClient loader.

Pre-existing `RegistryConfig` only modeled HTTPS ClawHub mirrors. The
plugin path adds:

  * `kind: plugin` — names an `oxenclaw.skill_sources` entry point
    instead of pointing at a URL. `url` is unused here.
  * `kind: clawhub` (default) — needs `url`. Validation catches the
    common typo of forgetting either.

The loader resolves the entry point lazily on first `get_client()`,
caches the instance, and verifies it satisfies SkillSourcePlugin
*before* handing it to downstream consumers — so a misimplemented
plugin fails at registry-load time with an actionable error rather
than at the first install attempt with a cryptic AttributeError.
"""

from __future__ import annotations

from typing import Any

import pytest

from oxenclaw.clawhub.registries import (
    ClawHubRegistries,
    MultiRegistryClient,
    PluginNotFoundError,
    RegistryConfig,
    _load_skill_source_plugin,
)
from oxenclaw.extensions.skill_source_demo.source import DemoSkillSource


# ─── RegistryConfig validation ────────────────────────────────────


def test_clawhub_kind_requires_url() -> None:
    with pytest.raises(ValueError, match="kind=clawhub requires a url"):
        RegistryConfig(name="x", kind="clawhub")


def test_plugin_kind_requires_plugin_name() -> None:
    with pytest.raises(ValueError, match="kind=plugin requires a `plugin` name"):
        RegistryConfig(name="x", kind="plugin")


def test_plugin_kind_url_is_optional() -> None:
    """Plugin entries don't speak HTTPS; url being None is fine."""
    cfg = RegistryConfig(name="samsung", kind="plugin", plugin="some_source")
    assert cfg.url is None
    assert cfg.options == {}


def test_plugin_kind_options_passes_through() -> None:
    cfg = RegistryConfig(
        name="samsung",
        kind="plugin",
        plugin="mx",
        options={"git_url": "ssh://git@example.com/x.git", "ssh_key_path": "~/.ssh/id"},
    )
    assert cfg.options["git_url"].startswith("ssh://")


def test_clawhub_kind_default() -> None:
    """Existing config.yaml files (kind absent) keep working."""
    cfg = RegistryConfig(name="public", url="https://clawhub.ai")
    assert cfg.kind == "clawhub"


# ─── _load_skill_source_plugin ────────────────────────────────────


class _FakeEntryPoint:
    def __init__(self, name: str, target: Any) -> None:
        self.name = name
        self._target = target

    def load(self) -> Any:
        return self._target


def test_load_plugin_returns_protocol_satisfier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve a plugin name to a SkillSourcePlugin via the fake
    entry-points iterable. We monkeypatch the importlib lookup so
    the test doesn't depend on whether the package is pip-installed."""
    fake = _FakeEntryPoint("demo", DemoSkillSource)
    monkeypatch.setattr(
        "oxenclaw.clawhub.registries.entry_points",
        lambda group: [fake] if group == "oxenclaw.skill_sources" else [],
        raising=False,
    )
    # The loader uses a local import; patch the module attribute
    # `importlib.metadata.entry_points` in-place instead.
    import importlib.metadata as md

    monkeypatch.setattr(
        md,
        "entry_points",
        lambda group: [fake] if group == "oxenclaw.skill_sources" else [],
    )
    src = _load_skill_source_plugin("demo", {})
    assert isinstance(src, DemoSkillSource)


def test_load_plugin_unknown_name_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.metadata as md

    monkeypatch.setattr(md, "entry_points", lambda group: [])
    with pytest.raises(PluginNotFoundError, match="not installed"):
        _load_skill_source_plugin("missing", {})


def test_load_plugin_not_callable_with_options_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plugin classes MUST take `options=` as a kwarg. A bare class
    that takes no kwargs surfaces the contract violation cleanly."""

    class _BadPlugin:
        def __init__(self) -> None:
            pass

    fake = _FakeEntryPoint("bad", _BadPlugin)
    import importlib.metadata as md

    monkeypatch.setattr(md, "entry_points", lambda group: [fake])
    with pytest.raises(TypeError, match="must accept `options"):
        _load_skill_source_plugin("bad", {})


def test_load_plugin_failing_protocol_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin that takes options but skips a protocol method must
    fail at load time, not at first call."""

    class _IncompletePlugin:
        def __init__(self, *, options: dict[str, Any]) -> None:
            self.options = options

        async def search_skills(self, query: str, *, limit: int | None = None):  # type: ignore[no-untyped-def]
            return []

        # missing list_skills, fetch_skill_detail, download_skill_archive, aclose

    fake = _FakeEntryPoint("incomplete", _IncompletePlugin)
    import importlib.metadata as md

    monkeypatch.setattr(md, "entry_points", lambda group: [fake])
    with pytest.raises(TypeError, match="does not satisfy"):
        _load_skill_source_plugin("incomplete", {})


# ─── MultiRegistryClient with mixed kinds ─────────────────────────


def test_multi_registry_resolves_clawhub_kind() -> None:
    cfg = ClawHubRegistries(
        default="public",
        registries=[RegistryConfig(name="public", url="https://clawhub.ai")],
    )
    multi = MultiRegistryClient(cfg)
    c1 = multi.get_client("public")
    c2 = multi.get_client("public")  # cached on second call
    assert c1 is c2


def test_multi_registry_resolves_plugin_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: registry config of kind=plugin, loader fetches the
    plugin via entry-points, MultiRegistryClient hands back the
    instance, second call returns the cached instance."""
    fake = _FakeEntryPoint("demo", DemoSkillSource)
    import importlib.metadata as md

    monkeypatch.setattr(md, "entry_points", lambda group: [fake])
    cfg = ClawHubRegistries(
        default="demo",
        registries=[
            RegistryConfig(
                name="demo", kind="plugin", plugin="demo", options={"x": 1}
            )
        ],
    )
    multi = MultiRegistryClient(cfg)
    c1 = multi.get_client("demo")
    c2 = multi.get_client("demo")
    assert isinstance(c1, DemoSkillSource)
    assert c1 is c2


def test_multi_registry_view_surfaces_kind() -> None:
    """The `skills.registries` RPC consumes view() — UIs need to know
    whether an entry is a URL mirror or a plugin so they can render the
    right hint."""
    cfg = ClawHubRegistries(
        default="public",
        registries=[
            RegistryConfig(name="public", url="https://clawhub.ai"),
            RegistryConfig(name="samsung", kind="plugin", plugin="mx_ai_skill_store"),
        ],
    )
    multi = MultiRegistryClient(cfg)
    rendered = {r["name"]: r for r in multi.view()}
    assert rendered["public"]["kind"] == "clawhub"
    assert rendered["samsung"]["kind"] == "plugin"
    assert rendered["samsung"]["plugin"] == "mx_ai_skill_store"


async def test_multi_registry_aclose_calls_each_loaded_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeEntryPoint("demo", DemoSkillSource)
    import importlib.metadata as md

    monkeypatch.setattr(md, "entry_points", lambda group: [fake])
    cfg = ClawHubRegistries(
        default="demo",
        registries=[
            RegistryConfig(name="demo", kind="plugin", plugin="demo")
        ],
    )
    multi = MultiRegistryClient(cfg)
    multi.get_client("demo")  # materialise
    await multi.aclose()
    # After aclose, the next get_client re-creates fresh — we verify
    # by checking the cache cleared (via private attr).
    assert multi._clients == {}
