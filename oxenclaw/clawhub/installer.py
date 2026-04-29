"""Skill installer pipeline: download → verify → extract → install.

Mirrors openclaw `src/agents/skills-clawhub.ts` (`performClawHubSkillInstall`).
We deliberately do NOT auto-run any `metadata.openclaw.install` specs (brew,
npm, go) — those describe binaries the user must already have. Auto-running
them inside the gateway would be a serious security hole. Required-binary
hints are surfaced via `SkillManifest.openclaw.requires` for the UI.
"""

from __future__ import annotations

import io
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from oxenclaw.clawhub.client import ClawHubClient, sha256_integrity
from oxenclaw.clawhub.desc_enricher import enrich_skill_description
from oxenclaw.clawhub.frontmatter import (
    VALID_SLUG_RE,
    SkillManifest,
    SkillManifestError,
    parse_skill_file,
)
from oxenclaw.clawhub.lockfile import Lockfile, OriginMetadata
from oxenclaw.clawhub.registries import MultiRegistryClient
from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.plugin_sdk.runtime_env import get_logger
from oxenclaw.security.skill_scanner import Finding, SkillScanner

logger = get_logger("clawhub.installer")


class InstallError(Exception):
    """Pipeline failure during install/uninstall/update."""


@dataclass
class InstallResult:
    slug: str
    version: str
    target_dir: Path
    integrity: str
    manifest: SkillManifest
    findings: list[Finding]
    registry_name: str | None = None
    registry_url: str | None = None
    trust: str | None = None


def _skill_dirs(paths: OxenclawPaths) -> tuple[Path, Path]:
    """Return (skills_root, lockfile_path)."""
    return paths.home / "skills", paths.home / ".clawhub" / "lock.json"


def _resolve_safe_target(skills_root: Path, slug: str) -> Path:
    """Compute the install dir for a slug, refusing path traversal attempts."""
    if not VALID_SLUG_RE.match(slug):
        raise InstallError(f"invalid slug: {slug!r}")
    target = (skills_root / slug).resolve()
    skills_root_abs = skills_root.resolve()
    try:
        target.relative_to(skills_root_abs)
    except ValueError as exc:
        raise InstallError(f"refusing to install outside skills root: {target}") from exc
    return target


def _extract_zip_to(temp_root: Path, archive_bytes: bytes) -> Path:
    """Unzip into `temp_root` and locate the directory containing SKILL.md."""
    temp_root.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
            for member in zf.namelist():
                # Reject absolute paths and parent traversal in archive entries.
                # zipfile already normalises by default in newer Python, but be
                # explicit here for defense in depth.
                if member.startswith(("/", "\\")) or ".." in Path(member).parts:
                    raise InstallError(f"archive contains unsafe path {member!r}")
            zf.extractall(temp_root)
    except zipfile.BadZipFile as exc:
        raise InstallError(f"downloaded archive is not a valid ZIP: {exc}") from exc
    skill_md_path = _find_skill_md(temp_root)
    if skill_md_path is None:
        raise InstallError("archive does not contain SKILL.md at any depth")
    return skill_md_path.parent


def _find_skill_md(root: Path) -> Path | None:
    """Return the shallowest SKILL.md inside `root` (or None)."""
    candidates = sorted(root.rglob("SKILL.md"), key=lambda p: len(p.parts))
    return candidates[0] if candidates else None


def _resolve_target_version(detail: dict, requested: str | None) -> str:
    if requested:
        return requested
    latest = detail.get("latestVersion") if isinstance(detail, dict) else None
    if isinstance(latest, dict) and latest.get("version"):
        return str(latest["version"])
    raise InstallError(
        "skill detail did not advertise a latest version and no version was requested"
    )


class SkillInstaller:
    """High-level install/uninstall/update orchestrator."""

    def __init__(
        self,
        client: ClawHubClient | MultiRegistryClient,
        *,
        paths: OxenclawPaths | None = None,
        scanner: SkillScanner | None = None,
        enrich_model: object | None = None,
        enrich_auth: object | None = None,
    ) -> None:
        # Accept either a single ClawHubClient (backward compat) or a
        # MultiRegistryClient. A bare ClawHubClient is treated as a
        # single-registry universe with no name; install() ignores
        # `registry=` in that mode.
        self._multi: MultiRegistryClient | None = (
            client if isinstance(client, MultiRegistryClient) else None
        )
        self._single: ClawHubClient | None = client if isinstance(client, ClawHubClient) else None
        self._paths = paths or default_paths()
        self._skills_root, self._lock_path = _skill_dirs(self._paths)
        self._scanner = scanner or SkillScanner()
        # Optional primary-LLM hooks for routing-hint enrichment. When
        # both are supplied, install() runs enrich_skill_description()
        # after the SKILL.md lands. When either is None, enrichment is
        # silently skipped — keeps unit tests / sandboxed boots working
        # without an Anthropic key.
        self._enrich_model = enrich_model
        self._enrich_auth = enrich_auth

    @property
    def multi(self) -> MultiRegistryClient | None:
        return self._multi

    def _resolve_client(self, registry: str | None) -> tuple[ClawHubClient, str | None, str | None]:
        """Return (client, registry_name_or_None, trust_or_None)."""
        if self._multi is not None:
            name = registry or self._multi.config.resolved_default()
            return (
                self._multi.get_client(name),
                name,
                self._multi.trust(name),
            )
        if self._single is not None:
            return self._single, None, None
        raise InstallError("no clawhub client configured")

    @property
    def skills_root(self) -> Path:
        return self._skills_root

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def lockfile(self) -> Lockfile:
        return Lockfile.load(self._lock_path)

    def is_installed(self, slug: str) -> bool:
        return (self._skills_root / slug / "SKILL.md").exists()

    async def install(
        self,
        slug: str,
        *,
        version: str | None = None,
        force: bool = False,
        allow_critical_findings: bool = False,
        registry: str | None = None,
    ) -> InstallResult:
        """Install a skill. On a clean scan we proceed; on critical scanner
        findings we refuse unless `allow_critical_findings=True`.
        """
        if not VALID_SLUG_RE.match(slug):
            raise InstallError(f"invalid slug: {slug!r}")

        target = _resolve_safe_target(self._skills_root, slug)
        if target.exists() and not force:
            raise InstallError(
                f"skill {slug!r} already installed at {target}. Pass force=True to overwrite."
            )

        try:
            client, registry_name, trust = self._resolve_client(registry)
        except KeyError as exc:
            raise InstallError(str(exc)) from exc

        detail = await client.fetch_skill_detail(slug)
        resolved_version = _resolve_target_version(detail, version)

        archive_bytes, integrity = await client.download_skill_archive(
            slug, version=resolved_version
        )
        # Belt-and-suspenders: re-hash and confirm we agree.
        verify = sha256_integrity(archive_bytes)
        if verify != integrity:
            raise InstallError("internal integrity check inconsistent — refusing install")

        # Extract to a fresh temp dir under the skills root so we never
        # leak files into /tmp on failure.
        staging = self._skills_root / ".staging" / slug
        if staging.exists():
            shutil.rmtree(staging)
        findings: list[Finding] = []
        try:
            source_dir = _extract_zip_to(staging, archive_bytes)
            manifest, body = parse_skill_file(source_dir / "SKILL.md")
            if manifest.name != slug:
                logger.warning(
                    "skill manifest name=%r does not match requested slug=%r — using slug for filesystem layout",
                    manifest.name,
                    slug,
                )

            findings = self._scanner.scan(manifest, body)
            if self._scanner.has_critical(findings) and not allow_critical_findings:
                summary = self._scanner.summarise(findings)
                raise InstallError(
                    f"refusing to install {slug!r}: scanner reported "
                    f"{summary['critical']} critical, {summary['warn']} warn, "
                    f"{summary['info']} info finding(s). "
                    "Pass allow_critical_findings=True to override."
                )

            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_dir, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

        # Provenance + lockfile bookkeeping.
        now = time.time()
        OriginMetadata(
            registry=client.base_url,
            registry_name=registry_name,
            trust=trust,
            slug=slug,
            installed_version=resolved_version,
            installed_at=now,
            integrity=integrity,
        ).save(target / ".clawhub" / "origin.json")

        lock = self.lockfile()
        lock.upsert(slug, resolved_version, installed_at=now)
        lock.save(self._lock_path)

        # Best-effort LLM routing-hint enrichment. Failures are logged
        # inside enrich_skill_description and never raise.
        if self._enrich_model is not None and self._enrich_auth is not None:
            try:
                await enrich_skill_description(
                    skill_dir=target,
                    name=manifest.name,
                    description=manifest.description,
                    body=body,
                    model=self._enrich_model,
                    auth=self._enrich_auth,
                )
            except Exception:
                logger.exception("desc enrichment raised for %s", slug)

        return InstallResult(
            slug=slug,
            version=resolved_version,
            target_dir=target,
            integrity=integrity,
            manifest=manifest,
            findings=findings,
            registry_name=registry_name,
            registry_url=client.base_url,
            trust=trust,
        )

    def uninstall(self, slug: str) -> bool:
        target = _resolve_safe_target(self._skills_root, slug)
        if not target.exists():
            # Still scrub the lockfile in case the directory was deleted manually.
            lock = self.lockfile()
            removed = lock.remove(slug)
            if removed:
                lock.save(self._lock_path)
            return False
        shutil.rmtree(target)
        lock = self.lockfile()
        lock.remove(slug)
        lock.save(self._lock_path)
        return True

    async def update(self, slug: str) -> InstallResult:
        if not self.is_installed(slug):
            raise InstallError(f"{slug!r} is not installed")
        # Re-target the same registry the skill came from when one is recorded;
        # otherwise fall back to the installer's default. This keeps mirror-
        # sourced installs from silently jumping to the public hub on update.
        origin_path = self._skills_root / slug / ".clawhub" / "origin.json"
        registry: str | None = None
        if origin_path.exists() and self._multi is not None:
            origin = OriginMetadata.load(origin_path)
            if origin and origin.registry_name and origin.registry_name in self._multi.names():
                registry = origin.registry_name
        return await self.install(slug, force=True, registry=registry)

    async def update_all(self) -> list[InstallResult]:
        out: list[InstallResult] = []
        for slug in list(self.lockfile().skills):
            try:
                out.append(await self.update(slug))
            except (InstallError, SkillManifestError) as exc:
                logger.warning("update %s failed: %s", slug, exc)
        return out
