"""`sampyclaw skills` — search/install/list/uninstall/update from ClawHub.

These commands talk to ClawHub directly (no running gateway needed). For
the same operations from a remote client, use the `skills.*` JSON-RPC
methods exposed by `gateway start`.

Multiple registries (verified mirrors) are supported via the `clawhub`
section of config.yaml. Pass `--registry NAME` to target a specific one,
or `--base-url URL` to point at an ad-hoc registry that overrides config.
"""

from __future__ import annotations

import asyncio
import json

import typer

from sampyclaw.clawhub import (
    ClawHubError,
    InstallError,
    MultiRegistryClient,
    SkillInstaller,
    load_installed_skills,
)
from sampyclaw.clawhub.registries import ClawHubRegistries, RegistryConfig
from sampyclaw.config import load_config
from sampyclaw.config.paths import default_paths

app = typer.Typer(
    help="Browse and install skills from ClawHub.",
    no_args_is_help=True,
)


def _multi_from_config_with_overrides(
    base_url: str | None, _registry: str | None
) -> MultiRegistryClient:
    """Build a MultiRegistryClient from config.yaml; if `--base-url` is given,
    treat it as an ad-hoc registry named `cli` and make it the default."""
    cfg_root = load_config()
    raw = (cfg_root.clawhub or {}) if hasattr(cfg_root, "clawhub") else {}
    try:
        registries = ClawHubRegistries.model_validate(raw or {})
    except Exception:
        registries = ClawHubRegistries()
    if base_url:
        registries = ClawHubRegistries(
            default="cli",
            registries=[
                RegistryConfig(name="cli", url=base_url, trust="mirror"),
                *registries.registries,
            ],
        )
    return MultiRegistryClient(registries)


@app.command("registries")
def registries() -> None:
    """List configured ClawHub registries (mirrors)."""
    multi = _multi_from_config_with_overrides(None, None)
    rows = multi.view()
    if not rows:
        typer.echo("(none)")
        return
    for r in rows:
        marker = "*" if r["default"] else " "
        token = "tok" if r["has_token"] else "   "
        typer.echo(f"  {marker} [{r['trust']:9s}] {token}  {r['name']:20s} {r['url']}")


@app.command("search")
def search(
    query: list[str] = typer.Argument(None, help="Optional query terms."),
    limit: int = typer.Option(20, "--limit", help="Max results."),
    base_url: str | None = typer.Option(
        None, "--base-url", help="Ad-hoc registry URL (overrides config)."
    ),
    registry: str | None = typer.Option(None, "--registry", help="Registry name from config.yaml."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
) -> None:
    """Search ClawHub skills."""

    async def _run() -> None:
        multi = _multi_from_config_with_overrides(base_url, registry)
        client = multi.get_client(registry)
        try:
            results = await client.search_skills(" ".join(query or []), limit=limit)
        finally:
            await multi.aclose()
        if json_output:
            typer.echo(json.dumps(results, indent=2, ensure_ascii=False))
            return
        if not results:
            typer.echo("(no results)")
            return
        for r in results:
            slug = r.get("slug", "?")
            name = r.get("displayName") or slug
            ver = r.get("version") or ""
            summary = r.get("summary") or ""
            typer.echo(f"  {slug:30s} {ver:10s} {name}")
            if summary:
                typer.echo(f"    {summary}")

    asyncio.run(_run())


@app.command("install")
def install(
    slug: str = typer.Argument(..., help="Skill slug."),
    version: str | None = typer.Option(None, "--version", help="Pin a version."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing install."),
    base_url: str | None = typer.Option(None, "--base-url", help="Ad-hoc registry URL."),
    registry: str | None = typer.Option(None, "--registry", help="Registry name from config.yaml."),
    allow_critical: bool = typer.Option(
        False, "--allow-critical", help="Override skill-scanner refusal."
    ),
) -> None:
    """Install a skill from ClawHub or a verified mirror."""

    async def _run() -> None:
        multi = _multi_from_config_with_overrides(base_url, registry)
        installer = SkillInstaller(multi, paths=default_paths())
        try:
            res = await installer.install(
                slug,
                version=version,
                force=force,
                allow_critical_findings=allow_critical,
                registry=registry or ("cli" if base_url else None),
            )
            typer.echo(
                f"installed {res.slug} v{res.version} from "
                f"{res.registry_name or '(default)'} ({res.trust or 'unknown trust'})"
            )
            typer.echo(f"  → {res.target_dir}")
            typer.echo(f"  integrity: {res.integrity}")
            req = res.manifest.openclaw.requires
            req_bins = list(req.bins) + list(req.any_bins)
            if req_bins:
                typer.echo(f"  requires on PATH: {', '.join(req_bins)}")
            if res.findings:
                typer.echo(f"  scanner findings: {len(res.findings)}")
        except (ClawHubError, InstallError) as exc:
            typer.echo(f"install failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            await multi.aclose()

    asyncio.run(_run())


@app.command("list")
def list_installed() -> None:
    """List locally installed skills."""
    skills = load_installed_skills()
    if not skills:
        typer.echo("(no skills installed)")
        return
    for s in skills:
        ver = s.origin.installed_version if s.origin else "?"
        emoji = (s.manifest.openclaw.emoji or "·") + " "
        reg = (s.origin.registry_name if s.origin else None) or "?"
        trust = (s.origin.trust if s.origin else None) or "?"
        typer.echo(f"  {emoji}{s.slug:30s} v{ver:10s} [{reg}/{trust}]  {s.description[:60]}")


@app.command("uninstall")
def uninstall(slug: str = typer.Argument(...)) -> None:
    """Remove an installed skill."""

    async def _run() -> None:
        multi = _multi_from_config_with_overrides(None, None)
        installer = SkillInstaller(multi, paths=default_paths())
        try:
            removed = installer.uninstall(slug)
        finally:
            await multi.aclose()
        if removed:
            typer.echo(f"removed {slug}")
        else:
            typer.echo(f"{slug} was not installed", err=True)
            raise typer.Exit(code=1)

    asyncio.run(_run())


@app.command("update")
def update(
    slug: str | None = typer.Argument(None, help="Single slug; omit to update all."),
    base_url: str | None = typer.Option(None, "--base-url"),
    registry: str | None = typer.Option(None, "--registry"),
) -> None:
    """Re-install at the latest version (each skill targets its own source registry)."""

    async def _run() -> None:
        multi = _multi_from_config_with_overrides(base_url, registry)
        installer = SkillInstaller(multi, paths=default_paths())
        try:
            if slug:
                res = await installer.update(slug)
                typer.echo(
                    f"updated {res.slug} → v{res.version} from {res.registry_name or '(default)'}"
                )
            else:
                results = await installer.update_all()
                if not results:
                    typer.echo("(nothing to update)")
                for r in results:
                    typer.echo(
                        f"updated {r.slug} → v{r.version} from {r.registry_name or '(default)'}"
                    )
        except (ClawHubError, InstallError) as exc:
            typer.echo(f"update failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            await multi.aclose()

    asyncio.run(_run())


@app.command("detail")
def detail(
    slug: str = typer.Argument(...),
    base_url: str | None = typer.Option(None, "--base-url"),
    registry: str | None = typer.Option(None, "--registry"),
) -> None:
    """Print raw ClawHub detail for a skill."""

    async def _run() -> None:
        multi = _multi_from_config_with_overrides(base_url, registry)
        try:
            data = await multi.get_client(registry).fetch_skill_detail(slug)
            typer.echo(json.dumps(data, indent=2, ensure_ascii=False))
        finally:
            await multi.aclose()

    asyncio.run(_run())
