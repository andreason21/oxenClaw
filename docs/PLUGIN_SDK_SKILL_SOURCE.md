# Skill-source plugin SDK

oxenClaw's default skill catalog is ClawHub (HTTPS API at
`https://clawhub.ai`, plus declared mirrors). Some operators need to
expose a *non-ClawHub* catalog — a private GitHub repo over SSH, an
internal artifact server, a corporate skill review system — without
landing the access details inside oxenClaw itself.

This page documents the third-party-package extension point that
covers that case. The plugin lives in its own (potentially private)
package; oxenClaw discovers it at boot via Python entry points.

## Architecture in one paragraph

`oxenclaw.plugin_sdk.skill_source_contract.SkillSourcePlugin` is a
`runtime_checkable` Protocol with five async methods:
`search_skills`, `list_skills`, `fetch_skill_detail`,
`download_skill_archive`, `aclose`. `oxenclaw.clawhub.client.ClawHubClient`
is the canonical implementation; any class structurally satisfying
the same five methods can substitute. `MultiRegistryClient` resolves
each entry in `clawhub.registries[*]` to one or the other based on
the entry's `kind` field — `clawhub` instantiates a real
ClawHubClient; `plugin` looks up a class registered under the
`oxenclaw.skill_sources` entry-point group.

## Authoring a plugin

### 1. Implement the protocol

```python
# your_pkg/source.py
from typing import Any
from oxenclaw.clawhub.client import sha256_integrity


class InternalSkillSource:
    """Private skill catalog accessible over SSH-based git."""

    def __init__(self, *, options: dict[str, Any]) -> None:
        # `options` is the per-registry block from config.yaml, passed
        # through verbatim — keep schema validation here, not at the
        # oxenClaw side, so plugin updates don't require core releases.
        self._git_url = options["git_url"]
        self._ssh_key_path = options.get("ssh_key_path")
        self._cache_dir = options.get("cache_dir") or "~/.oxenclaw/plugin-cache"
        # ... open git working copy, set up GIT_SSH_COMMAND, etc.

    async def search_skills(self, query, *, limit=None):
        # Walk the cached repo, filter by name/summary, return dicts
        # with at least {"slug": "..."}. `displayName`, `summary`,
        # `version` are optional but improve the dashboard UX.
        ...

    async def list_skills(self, *, limit=None):
        # Return {"results": [{slug, ...}, ...], "filtered_count": 0}.
        ...

    async def fetch_skill_detail(self, slug):
        # Return at minimum {"latestVersion": {"version": "<tag>"}}.
        # Anything else gets surfaced on the detail panel.
        ...

    async def download_skill_archive(self, slug, *, version=None):
        # Build a ZIP whose root is `<slug>/SKILL.md`. The installer
        # rejects archives without that layout. Return
        # (bytes, integrity) where integrity = sha256_integrity(bytes).
        archive_bytes = ...  # build via zipfile.ZipFile
        return archive_bytes, sha256_integrity(archive_bytes)

    async def aclose(self):
        # Release any resources you hold (git working copy, HTTP
        # session, …). Must be safe to call multiple times.
        ...
```

The class MUST take `options` as a keyword-only arg in `__init__` —
the loader rejects plugins that don't, with a clear error.

### 2. Register the entry point

In your plugin package's `pyproject.toml`:

```toml
[project.entry-points."oxenclaw.skill_sources"]
internal_skill_store = "your_pkg.source:InternalSkillSource"
```

The left-hand-side name (`internal_skill_store`) is what users will
reference in `config.yaml`. The right-hand side is the dotted
`module:class` path the loader imports.

### 3. Operator config

```yaml
clawhub:
  default: internal
  registries:
    - name: internal
      kind: plugin
      plugin: internal_skill_store
      trust: official       # or 'mirror' / 'community'
      options:
        git_url: ssh://git@git.example.com/org/skill-store.git
        ssh_key_path: ~/.ssh/id_ed25519_internal
        cache_dir: ~/.oxenclaw/plugin-cache
    - name: public
      url: https://clawhub.ai
      trust: official
```

`url` / `token` / `token_env` are unused for `kind: plugin` entries
and may be omitted.

## Reference implementation

A working in-memory plugin lives under
`oxenclaw/extensions/skill_source_demo/source.py` (registered as
`oxenclaw_skill_source_demo`). It serves a single fake skill so the
SDK plumbing is exercised end-to-end. Read that file alongside this
page when starting a new plugin — its structure is the minimum
viable shape and it stays in sync with the protocol via the
`tests/test_plugin_sdk_skill_source.py` round-trip test.

## What plugins do *not* need to handle

* **Lockfile / origin metadata** — `SkillInstaller` writes
  `.clawhub/origin.json` after a successful install regardless of
  source kind.
* **SkillManifest parsing** — the installer parses the SKILL.md
  inside the archive you return; you only need to package the right
  files.
* **Compat checks / scanner findings** — the installer runs both
  pipelines on the extracted archive. Plugins that pre-validate are
  welcome to refuse to serve broken skills, but it's not required.
* **Auto-installing required binaries** — the bin-install plan is
  derived from the manifest's `metadata.openclaw.install` block by
  oxenClaw itself; plugins don't surface a separate install path.

## What plugins *should* be careful about

* **Threading credentials**. The plugin owns the credential surface
  for its source — SSH keys, OAuth tokens, internal API headers.
  oxenClaw never sees them. Validate the `options` dict carefully
  and log credential lookups (not values) so misconfiguration is
  diagnosable.

* **Archive shape**. The ZIP root MUST contain `<slug>/SKILL.md`
  exactly. The installer's path-traversal guard rejects archives
  with `..` segments or absolute paths; build the ZIP with relative
  member names only.

* **Cache invalidation**. Most plugins will cache a git working
  copy or an HTTP response. Choose a cache key that reflects both
  slug AND version; `download_skill_archive(slug, version="2.0")`
  must return the 2.0 archive, not whatever's currently cached for
  `slug`.

* **`aclose()` is idempotent**. The gateway calls it on shutdown;
  some test fixtures call it more than once. Don't raise if there's
  nothing to close.
