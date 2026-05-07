"""Reference `SkillSourcePlugin` implementation — in-memory, no I/O.

Real skill-source plugins (private corporate catalogs, alternative
HTTP/SSH backends, …) live in their own packages. This bundled demo
exists so:

  * The plugin SDK is exercised end-to-end at oxenClaw's own test
    boundary, not just at unit level — `oxenclaw skills install
    --base-url ...` doesn't apply, but a `kind: plugin` registry
    entry pointing at this demo demonstrates the full path through
    `MultiRegistryClient` → `SkillInstaller` → `_extract_zip_to`.

  * Authors of new plugins have a working <100-LOC reference to read
    when wiring their own SSH/HTTP/whatever skill source. Copy this
    file, replace the in-memory dict with real fetches, ship.

Operators do not need to add this registry to `config.yaml` — it is
not auto-registered. To poke at it during development:

    clawhub:
      registries:
        - name: demo
          kind: plugin
          plugin: oxenclaw_skill_source_demo
          trust: community
"""

from oxenclaw.extensions.skill_source_demo.source import DemoSkillSource

__all__ = ["DemoSkillSource"]
