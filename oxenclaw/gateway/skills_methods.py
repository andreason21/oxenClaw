"""skills.* JSON-RPC methods bound to a ClawHubClient + SkillInstaller.

Mirrors openclaw `src/cli/skills-cli.ts` + ClawHub package operations,
exposed over the gateway so the dashboard / external clients can browse
and install skills. Supports either a single ClawHubClient (legacy) or a
MultiRegistryClient when operators have configured multiple verified
mirrors in `clawhub.registries`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from oxenclaw.clawhub import (
    ClawHubClient,
    ClawHubError,
    InstallError,
    MultiRegistryClient,
    SkillInstaller,
    load_installed_skills,
)
from oxenclaw.clawhub.compat import check_skill_dict_compatibility
from oxenclaw.clawhub.frontmatter import serialise_install_specs
from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.gateway.router import Router


class _SearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = ""
    limit: int | None = None
    registry: str | None = None
    # When True, results that fail the environment-compatibility
    # probe (wrong OS, missing required bins/env vars) are still
    # returned (annotated with `compat`). Default False filters them
    # out so the dashboard catalog only surfaces things the operator
    # could actually install + run on this machine.
    include_incompatible: bool = False


class _SlugParam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str
    registry: str | None = None


class _InstallParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str
    version: str | None = None
    force: bool = False
    allow_critical_findings: bool = False
    registry: str | None = None


def _annotate_and_filter(
    results: list[dict[str, Any]], *, include_incompatible: bool
) -> list[dict[str, Any]]:
    """Attach `compat` to every result and drop incompatible ones
    unless the caller opted in to see them. Pure function — easy to
    unit-test against a constructed payload."""
    out: list[dict[str, Any]] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        report = check_skill_dict_compatibility(r)
        annotated = {**r, "compat": report.to_dict()}
        if not include_incompatible and not report.installable:
            continue
        out.append(annotated)
    return out


def _installed_view(paths: OxenclawPaths) -> list[dict[str, Any]]:
    # `skills.list_installed` is about what the user explicitly installed
    # via ClawHub — it powers the dashboard's Skills view and the
    # uninstall flow. Bundled skills are available to the model but
    # don't belong here (you can't uninstall them).
    skills = load_installed_skills(paths, include_bundled=False)
    out: list[dict[str, Any]] = []
    for s in skills:
        # Re-check compat at view time so the dashboard surfaces
        # "installed but no longer runnable" cases (deleted bin,
        # cleared env var) without an explicit reinstall step.
        compat = check_skill_dict_compatibility(
            {"openclaw": s.manifest.openclaw.model_dump(by_alias=True)}
        ).to_dict()
        out.append(
            {
                "slug": s.slug,
                "name": s.name,
                "description": s.description,
                "version": s.origin.installed_version if s.origin else None,
                "installed_at": s.origin.installed_at if s.origin else None,
                "registry": s.origin.registry if s.origin else None,
                "registry_name": s.origin.registry_name if s.origin else None,
                "trust": s.origin.trust if s.origin else None,
                "skill_md_path": str(s.skill_md_path),
                "homepage": s.manifest.homepage,
                "emoji": s.manifest.openclaw.emoji,
                "requires": s.manifest.openclaw.requires.model_dump(by_alias=True),
                "install_specs": serialise_install_specs(s.manifest.openclaw.install),
                "compat": compat,
            }
        )
    return out


def register_skills_methods(
    router: Router,
    *,
    client: ClawHubClient | MultiRegistryClient,
    installer: SkillInstaller | None = None,
    paths: OxenclawPaths | None = None,
) -> None:
    resolved_paths = paths or default_paths()
    resolved_installer = installer or SkillInstaller(client, paths=resolved_paths)

    multi: MultiRegistryClient | None = client if isinstance(client, MultiRegistryClient) else None
    single: ClawHubClient | None = client if isinstance(client, ClawHubClient) else None

    def _client_for(registry: str | None) -> ClawHubClient:
        if multi is not None:
            return multi.get_client(registry)
        return single  # type: ignore[return-value]

    def _wrap(exc: Exception) -> dict[str, Any]:
        if isinstance(exc, ClawHubError):
            return {"ok": False, "error": str(exc), "status": exc.status}
        return {"ok": False, "error": str(exc)}

    @router.method("skills.registries")
    async def _registries(_: dict) -> dict[str, Any]:  # type: ignore[type-arg]
        if multi is not None:
            return {
                "registries": multi.view(),
                "default": multi.config.resolved_default(),
            }
        return {
            "registries": [
                {
                    "name": "(single)",
                    "url": single.base_url if single else "",
                    "trust": "official",
                    "default": True,
                    "has_token": False,
                }
            ],
            "default": "(single)",
        }

    @router.method("skills.search", _SearchParams)
    async def _search(p: _SearchParams) -> dict[str, Any]:  # type: ignore[type-arg]
        try:
            results = await _client_for(p.registry).search_skills(p.query, limit=p.limit)
        except (ClawHubError, KeyError) as exc:
            return _wrap(exc)
        annotated = _annotate_and_filter(results, include_incompatible=p.include_incompatible)
        return {
            "ok": True,
            "registry": p.registry,
            "results": annotated,
            "filtered_count": len(results) - len(annotated),
        }

    @router.method("skills.list_remote", _SearchParams)
    async def _list_remote(p: _SearchParams) -> dict[str, Any]:  # type: ignore[type-arg]
        try:
            data = await _client_for(p.registry).list_skills(limit=p.limit)
        except (ClawHubError, KeyError) as exc:
            return _wrap(exc)
        results = data.get("results") if isinstance(data, dict) else None
        results = list(results or [])
        annotated = _annotate_and_filter(results, include_incompatible=p.include_incompatible)
        out = {**data, "results": annotated} if isinstance(data, dict) else {"results": annotated}
        out["filtered_count"] = len(results) - len(annotated)
        return {"ok": True, "registry": p.registry, **out}

    @router.method("skills.detail", _SlugParam)
    async def _detail(p: _SlugParam) -> dict[str, Any]:  # type: ignore[type-arg]
        try:
            data = await _client_for(p.registry).fetch_skill_detail(p.slug)
        except (ClawHubError, KeyError) as exc:
            return _wrap(exc)
        # Detail always carries compat — we never hide a slug the
        # caller asked about by name; they wouldn't see why.
        compat = check_skill_dict_compatibility(data).to_dict()
        return {
            "ok": True,
            "registry": p.registry,
            "detail": data,
            "compat": compat,
        }

    @router.method("skills.list_installed")
    async def _list_installed(_: dict) -> dict[str, Any]:  # type: ignore[type-arg]
        return {"ok": True, "skills": _installed_view(resolved_paths)}

    @router.method("skills.install", _InstallParams)
    async def _install(p: _InstallParams) -> dict[str, Any]:  # type: ignore[type-arg]
        try:
            result = await resolved_installer.install(
                p.slug,
                version=p.version,
                force=p.force,
                allow_critical_findings=p.allow_critical_findings,
                registry=p.registry,
            )
        except (ClawHubError, InstallError) as exc:
            return {"ok": False, "error": str(exc)}
        findings = [
            {
                "rule": f.rule,
                "severity": f.severity.value,
                "message": f.message,
                "location": f.location,
                "snippet": f.snippet,
            }
            for f in result.findings
        ]
        return {
            "ok": True,
            "slug": result.slug,
            "version": result.version,
            "registry": result.registry_name,
            "registry_url": result.registry_url,
            "trust": result.trust,
            "target_dir": str(result.target_dir),
            "integrity": result.integrity,
            "findings": findings,
            "manifest": {
                "name": result.manifest.name,
                "description": result.manifest.description,
                "homepage": result.manifest.homepage,
                "emoji": result.manifest.openclaw.emoji,
                "requires": result.manifest.openclaw.requires.model_dump(by_alias=True),
                "install_specs": serialise_install_specs(result.manifest.openclaw.install),
            },
        }

    @router.method("skills.scan", _SlugParam)
    async def _scan(p: _SlugParam) -> dict[str, Any]:  # type: ignore[type-arg]
        """Pre-install scan: download archive, extract, scan, but don't install."""
        try:
            c = _client_for(p.registry)
            detail = await c.fetch_skill_detail(p.slug)
            import shutil

            from oxenclaw.clawhub.frontmatter import parse_skill_file
            from oxenclaw.clawhub.installer import _extract_zip_to, _resolve_target_version
            from oxenclaw.security import SkillScanner

            version = _resolve_target_version(detail, None)
            archive, _ = await c.download_skill_archive(p.slug, version=version)
            staging = resolved_paths.home / "skills" / ".scan-staging" / p.slug
            if staging.exists():
                shutil.rmtree(staging)
            try:
                source_dir = _extract_zip_to(staging, archive)
                manifest, body = parse_skill_file(source_dir / "SKILL.md")
                findings = SkillScanner().scan(manifest, body)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
        except (ClawHubError, InstallError, KeyError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        return {
            "ok": True,
            "slug": p.slug,
            "version": version,
            "registry": p.registry,
            "findings": [
                {
                    "rule": f.rule,
                    "severity": f.severity.value,
                    "message": f.message,
                    "location": f.location,
                    "snippet": f.snippet,
                }
                for f in findings
            ],
            "summary": SkillScanner().summarise(findings),
        }

    @router.method("skills.uninstall", _SlugParam)
    async def _uninstall(p: _SlugParam) -> dict[str, Any]:  # type: ignore[type-arg]
        removed = resolved_installer.uninstall(p.slug)
        return {"ok": True, "removed": removed}

    @router.method("skills.update", _SlugParam)
    async def _update(p: _SlugParam) -> dict[str, Any]:  # type: ignore[type-arg]
        try:
            result = await resolved_installer.update(p.slug)
        except (ClawHubError, InstallError) as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "slug": result.slug,
            "version": result.version,
            "registry": result.registry_name,
            "trust": result.trust,
        }
