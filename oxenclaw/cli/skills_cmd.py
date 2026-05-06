"""`oxenclaw skills` — search/install/list/uninstall/update from ClawHub.

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
import shutil

import typer

from oxenclaw.clawhub import (
    ClawHubError,
    InstallError,
    MultiRegistryClient,
    SkillInstaller,
    load_installed_skills,
)
from oxenclaw.clawhub.bin_installer import (
    PlannedStep,
    StepResult,
    execute as execute_bin_plan,
    find_installed_skill,
    format_plan_preview,
    plan_install,
)
from oxenclaw.clawhub.registries import ClawHubRegistries, RegistryConfig
from oxenclaw.config import load_config
from oxenclaw.config.paths import default_paths

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


def _print_plan_summary(results: list[StepResult]) -> bool:
    """Emit the post-execute summary line + per-failure details.
    Returns True if every executed step exited 0 (or no steps ran)."""
    ran_ok = sum(1 for r in results if r.executed and r.exit_code == 0)
    failed = sum(1 for r in results if r.executed and (r.exit_code or 0) != 0)
    skipped = sum(1 for r in results if not r.executed)
    typer.echo(f"\nsummary: {ran_ok} ok, {failed} failed, {skipped} skipped")
    for r in results:
        if r.executed and (r.exit_code or 0) != 0:
            tail = (r.stderr_tail or "").replace("\n", " | ")
            typer.echo(
                f"  ✗ {r.step.label}: exit={r.exit_code} {tail}", err=True
            )
    return failed == 0


class _AutoApprovePrompter:
    """Prompter for the post-install batch flow: the user already
    confirmed once at the umbrella level, so each step auto-runs."""

    def confirm(self, step: PlannedStep) -> bool:
        return True

    def notify(self, message: str) -> None:
        typer.echo("  " + message)


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
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help=(
            "Auto-confirm the post-install bin-install prompt. Skill "
            "files are always installed unconditionally; this flag only "
            "affects the bin-dependency follow-up."
        ),
    ),
    no_bins: bool = typer.Option(
        False,
        "--no-bins",
        help=(
            "Skip the post-install bin-install prompt entirely. Useful "
            "for CI / scripted installs where the operator will handle "
            "binaries separately."
        ),
    ),
) -> None:
    """Install a skill from ClawHub or a verified mirror.

    After the skill files are installed, if the manifest declares
    binary dependencies that are not yet on PATH AND the manifest
    ships an install plan, the command previews the plan and asks
    once whether to install everything in a single batch. This
    consolidates the previous two-step `install` + `install-bins`
    into one "install + (preview) → confirm → install" flow so the
    operator never has to remember to chain the second command.

    Refused install kinds (`exec`, `download`) are surfaced in the
    preview with their original spec so they can be run by hand.
    apt steps that need root will fail with permission denied — the
    summary makes that visible per-step, and the operator can re-run
    the bin install under sudo via `oxenclaw skills install-bins`.
    """

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
            missing: list[str] = []
            if req_bins:
                typer.echo(f"  requires on PATH: {', '.join(req_bins)}")
                missing = [b for b in req_bins if shutil.which(b) is None]
            if res.findings:
                typer.echo(f"  scanner findings: {len(res.findings)}")
        except (ClawHubError, InstallError) as exc:
            typer.echo(f"install failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            await multi.aclose()

        if no_bins or not missing:
            if missing:
                # User opted out of auto-flow; surface manual recipe.
                typer.echo(
                    f"  missing: {', '.join(missing)}  →  "
                    f"`oxenclaw skills install-bins {res.slug}` "
                    "to install with explicit per-step confirm"
                )
            return

        plan = plan_install(res.manifest)
        runnable = [s for s in plan if s.decision == "run"]
        if not runnable:
            # Manifest declares missing bins but ships no auto-runnable
            # spec (or every spec is exec/download/refused). Print the
            # original guidance so the user knows where to look.
            typer.echo(
                f"  missing: {', '.join(missing)}  →  no auto-installable "
                "specs in the manifest; install manually using the install "
                "block in SKILL.md"
            )
            if plan:
                typer.echo("  install steps documented by the skill:")
                typer.echo(format_plan_preview(plan))
            return

        typer.echo("")
        typer.echo(f"missing binaries: {', '.join(missing)}")
        typer.echo("the skill ships these install steps:")
        typer.echo(format_plan_preview(plan))
        typer.echo("")
        if not yes:
            proceed = typer.confirm(
                "install required binaries now?", default=True
            )
            if not proceed:
                typer.echo(
                    f"skipped. Run `oxenclaw skills install-bins {res.slug}` "
                    "later for a step-by-step install."
                )
                return
        results = execute_bin_plan(plan, _AutoApprovePrompter())
        all_ok = _print_plan_summary(results)
        if not all_ok:
            raise typer.Exit(code=1)

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


@app.command("install-bins")
def install_bins(
    slug: str = typer.Argument(..., help="Slug of an already-installed skill."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-confirm every step (skip prompts)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the per-step plan without executing."
    ),
) -> None:
    """Run a skill's `metadata.openclaw.install` steps with per-step confirm.

    The base `install` command never executes brew/apt/npm specs — those
    describe binaries the user must already have on PATH. This command
    is the explicit opt-in: walk the install plan, ask before each step,
    and run only confirmed steps.

    Refused kinds in v1: `exec` (arbitrary shell), `download` (arbitrary
    URL). The original spec is printed so you can run it manually if you
    want. apt steps that need root will fail with permission denied;
    re-run the CLI under sudo or invoke them by hand.
    """
    skill = find_installed_skill(slug)
    if skill is None:
        typer.echo(f"skill {slug!r} is not installed", err=True)
        raise typer.Exit(code=1)
    plan = plan_install(skill.manifest)
    if not plan:
        typer.echo(f"{slug}: no install steps declared (nothing to do)")
        return
    typer.echo(f"will run for skill {slug!r}:")

    class _CliPrompter:
        def confirm(self, step: PlannedStep) -> bool:
            if yes:
                return True
            return typer.confirm("  proceed?", default=False)

        def notify(self, message: str) -> None:
            typer.echo("  " + message)

    results = execute_bin_plan(plan, _CliPrompter(), dry_run=dry_run)
    if not _print_plan_summary(results):
        raise typer.Exit(code=1)


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
