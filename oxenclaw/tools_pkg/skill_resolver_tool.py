"""skill_resolver tool — intent detection → skill lookup → install → guidance.

The LLM calls this when the user's request implies a domain the agent
doesn't already know how to handle. The tool:

1. Checks installed skills (bundled + user) for a name/description match.
2. Falls back to a registry search via `MultiRegistryClient` when available.
3. Optionally installs the top match via `SkillInstaller`.
4. Returns the SKILL.md path + first 600 chars of its body so the LLM
   can follow the documented scripts via the shell tool.

Pass `registries=None` and `installer=None` for test / dev mode — only the
installed-skill path works, which is still useful for local introspection.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, model_validator

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.clawhub.loader import InstalledSkill, load_installed_skills
from oxenclaw.clawhub.parallel_search import parallel_search_sources
from oxenclaw.clawhub.sources.base import SkillSource
from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.tools_pkg._arg_aliases import fold_aliases


class _Args(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _absorb(cls, data: Any) -> Any:
        return fold_aliases(
            data,
            {
                "query": ("q", "search", "text", "prompt", "topic", "intent", "task"),
            },
        )

    query: str = Field(
        ...,
        description=(
            "Short phrase describing the user's intent "
            "(e.g. 'weather in Seoul', 'analyze AAPL stock')."
        ),
    )
    auto_install: bool = Field(
        True,
        description="If true, install the top match when not yet installed.",
    )


def _skill_payload(skill: InstalledSkill) -> dict[str, Any]:
    skill_dir = skill.skill_md_path.parent
    scripts_dir = skill_dir / "scripts"
    return {
        "found": "installed",
        "slug": skill.slug,
        "name": skill.name,
        "skill_md": str(skill.skill_md_path),
        "scripts_dir": str(scripts_dir) if scripts_dir.exists() else None,
        "instructions": skill.body[:600],
    }


def _match_installed(skills: list[InstalledSkill], query: str) -> InstalledSkill | None:
    q = query.lower()
    words = q.split()
    # Exact slug / name match first.
    for s in skills:
        if s.slug.lower() == q or s.name.lower() == q:
            return s
    # Substring match: the full query OR any individual word in slug/name/description.
    for s in skills:
        slug_l = s.slug.lower()
        name_l = s.name.lower()
        desc_l = s.description.lower()
        if q in slug_l or q in name_l or q in desc_l:
            return s
        # Word-level: all query words appear somewhere in name+description.
        combined = f"{name_l} {desc_l} {slug_l}"
        if words and all(w in combined for w in words):
            return s
    return None


def _score_remote(result: dict[str, Any], query: str) -> float:
    q = query.lower()
    words = q.split()
    name = (result.get("name") or result.get("slug") or "").lower()
    desc = (result.get("description") or "").lower()
    if name == q:
        return 1.0
    if q in name or q in desc:
        return 0.5
    # Word-level: all query words appear somewhere in the combined text.
    combined = f"{name} {desc}"
    if words and all(w in combined for w in words):
        return 0.5
    return 0.0


def skill_resolver_tool(
    *,
    registries: Any | None = None,  # MultiRegistryClient | None
    installer: Any | None = None,  # SkillInstaller | None
    paths: OxenclawPaths | None = None,
    extra_sources: list[SkillSource] | None = None,
) -> Tool:
    """Return a FunctionTool named ``skill_resolver``.

    Parameters
    ----------
    registries:
        A ``MultiRegistryClient`` (or compatible) for remote search.
        Pass ``None`` to restrict to installed-skill matching only.
    installer:
        A ``SkillInstaller`` for downloading and extracting a skill.
        Ignored when ``registries`` is ``None``.
    paths:
        Override the default ``~/.oxenclaw/`` layout.
    """
    resolved_paths = paths or default_paths()

    async def _handler(args: _Args) -> str:
        query = args.query.strip()
        if not query:
            return json.dumps(
                {"found": "error", "error": "query must not be empty", "step": "validate"}
            )

        # ── 1. Check installed skills ─────────────────────────────────────
        try:
            installed = load_installed_skills(resolved_paths)
        except Exception as exc:
            return json.dumps({"found": "error", "error": str(exc), "step": "list_installed"})

        match = _match_installed(installed, query)
        if match is not None:
            return json.dumps(_skill_payload(match))

        # ── 2. Remote search via parallel SkillSource fan-out ────────────
        sources: list[SkillSource] = []
        if registries is not None:
            try:
                from oxenclaw.clawhub.sources.clawhub import ClawHubSource

                sources.append(ClawHubSource(registries))
            except Exception:
                pass
        try:
            from oxenclaw.clawhub.sources.github import GitHubSource
            from oxenclaw.clawhub.sources.index import IndexSource

            sources.append(GitHubSource())
            idx = IndexSource()
            if idx.configured:
                sources.append(idx)
        except Exception:
            pass
        if extra_sources:
            sources.extend(extra_sources)

        if not sources:
            return json.dumps({"found": "none", "searched": query, "registries": []})

        try:
            refs = parallel_search_sources(sources, query, limit=10)
        except Exception as exc:
            return json.dumps({"found": "error", "error": str(exc), "step": "search"})

        # Adapt SkillRef → dict so the existing scoring / install path still works.
        results: list[dict[str, Any]] = [
            {
                "slug": ref.slug,
                "name": ref.slug,
                "description": ref.description,
                "source_id": ref.source_id,
                "trust": ref.trust_level,
            }
            for ref in refs
        ]

        def _registry_names() -> list[str]:
            if registries is None:
                return []
            try:
                return list(registries.names())
            except Exception:
                return []

        if not results:
            return json.dumps({"found": "none", "searched": query, "registries": _registry_names()})

        # Pick the top-scored result.
        scored = [(r, _score_remote(r, query)) for r in results]
        scored.sort(key=lambda x: x[1], reverse=True)
        top_result, top_score = scored[0]

        if top_score < 0.5:
            return json.dumps({"found": "none", "searched": query, "registries": _registry_names()})

        slug: str = top_result.get("slug") or top_result.get("name") or ""
        if not slug:
            return json.dumps({"found": "none", "searched": query, "registries": []})

        # ── 3. Auto-install ───────────────────────────────────────────────
        if args.auto_install and installer is not None:
            try:
                await installer.install(slug, force=False, allow_critical_findings=False)
            except Exception as exc:
                err = str(exc)
                # If already installed, that's fine — load it below.
                if "already installed" not in err:
                    return json.dumps({"found": "error", "error": err, "step": "install"})

        # Re-load installed skills after the install to get the full payload.
        try:
            installed_after = load_installed_skills(resolved_paths)
        except Exception as exc:
            return json.dumps({"found": "error", "error": str(exc), "step": "reload"})

        match_after = _match_installed(installed_after, slug)
        if match_after is not None:
            return json.dumps(_skill_payload(match_after))

        # Install was skipped (auto_install=False or no installer) — return
        # partial remote metadata so the LLM knows what's available.
        return json.dumps(
            {
                "found": "remote_only",
                "slug": slug,
                "name": top_result.get("name", slug),
                "description": top_result.get("description", ""),
                "score": top_score,
                "install_hint": f"Call skill_resolver again with auto_install=true to install '{slug}'.",
            }
        )

    return FunctionTool(
        name="skill_resolver",
        description=(
            "Detect the user's intent and find a matching skill from the local "
            "skill library or remote ClawHub registries. Installs the skill "
            "automatically when auto_install=true (default) and returns the "
            "path to SKILL.md plus the first 600 characters of its usage "
            "instructions so you can invoke the documented scripts via the "
            "shell tool."
        ),
        input_model=_Args,
        handler=_handler,
    )


__all__ = ["skill_resolver_tool"]
