"""Plugin SDK — alternative skill sources beyond ClawHub.

The default `oxenclaw skills install` flow talks HTTPS to a ClawHub
registry (or one of its mirrors). For internal / corporate skill
catalogs that do NOT speak the ClawHub API — a private GitHub repo
pulled via SSH, an artifact server, an in-house GraphQL endpoint —
the registry would otherwise have to be reimplemented inside
oxenClaw, leaking proprietary access details into the open repo.

This module exposes the contract a third-party package implements
to plug a non-ClawHub skill source into oxenClaw without modifying
oxenclaw itself. The plugin lives in its own (potentially private)
package, declares an entry point, and the gateway picks it up at
boot time.

Layout:

    [project.entry-points."oxenclaw.skill_sources"]
    internal_skill_store = "your_pkg.source:InternalSkillSource"

The entry point resolves to a *class* whose `__init__(*, options)`
takes the per-registry options dict from `config.yaml`. Operators
then declare a registry of `kind: plugin`:

    clawhub:
      registries:
        - name: internal
          kind: plugin
          plugin: internal_skill_store
          trust: official
          options:
            git_url: ssh://git@git.example.com/org/skill-store.git
            ssh_key_path: ~/.ssh/id_ed25519_internal

The plugin implementation must structurally satisfy
`SkillSourcePlugin` — `runtime_checkable` so a raw `isinstance` check
on the loaded object reliably catches missing methods at boot.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# Entry-point group third-party packages register against. Kept
# separate from the existing `oxenclaw.plugins` group (which is for
# Channel plugins like Slack) so the discovery flows don't have to
# discriminate by manifest shape.
SKILL_SOURCE_ENTRY_POINT_GROUP = "oxenclaw.skill_sources"


@runtime_checkable
class SkillSourcePlugin(Protocol):
    """Behavioural contract a skill-source plugin must satisfy.

    The four async methods mirror `oxenclaw.clawhub.client.ClawHubClient`
    one-for-one — that's deliberate. The rest of oxenClaw (SkillInstaller,
    skills.* JSON-RPC methods, the `oxenclaw skills` CLI) consumes this
    interface, and ClawHubClient itself is one valid implementation.

    Required behaviour per method:

    * `search_skills(query, limit)` — return a list of dicts with at
      minimum a `slug` key. Each dict is shown by the dashboard's
      Browse tab; extra keys (`displayName`, `summary`, `version`)
      improve the UX but are optional.

    * `list_skills(limit)` — return a dict shaped like
      `{"results": [{...}], "filtered_count": int, …}`. `results`
      is the same per-skill dict shape as `search_skills`.

    * `fetch_skill_detail(slug)` — return a dict carrying at least
      `latestVersion: {"version": "<semver-or-tag>"}`. The version
      string is what `download_skill_archive` will be called with.

    * `download_skill_archive(slug, version)` — return
      `(bytes, integrity_str)` where `bytes` is a ZIP archive whose
      root contains the skill directory (`<slug>/SKILL.md`, plus
      anything the skill ships) and `integrity_str` is the
      `sha256-<hex>` form returned by
      `oxenclaw.clawhub.client.sha256_integrity(bytes)`. The installer
      verifies integrity; mismatches abort the install.

    * `aclose()` — release any held resources (HTTP session, git
      working copy, etc.). Called from `MultiRegistryClient.aclose`
      at gateway shutdown.

    Plugins MAY add helper methods beyond the protocol — those won't
    be called by oxenClaw's core, but are useful for plugin-specific
    tooling. The runtime-checkable check only verifies the five
    required methods exist.
    """

    async def search_skills(
        self, query: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]: ...

    async def list_skills(
        self, *, limit: int | None = None
    ) -> dict[str, Any]: ...

    async def fetch_skill_detail(self, slug: str) -> dict[str, Any]: ...

    async def download_skill_archive(
        self, slug: str, *, version: str | None = None
    ) -> tuple[bytes, str]: ...

    async def aclose(self) -> None: ...


__all__ = [
    "SKILL_SOURCE_ENTRY_POINT_GROUP",
    "SkillSourcePlugin",
]
