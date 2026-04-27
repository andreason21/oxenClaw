"""Wiki markdown serialization — frontmatter + slugify + related blocks.

Mirrors openclaw `memory-wiki/src/markdown.ts`. The frontmatter is YAML
when PyYAML is available; otherwise we use a minimal in-house dump that
covers the shapes WikiPage uses (no nested dicts beyond claims/evidence).
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from oxenclaw.wiki.models import (
    WikiClaim,
    WikiEvidence,
    WikiPage,
    parse_wiki_page_kind,
)

try:
    import yaml as _yaml  # type: ignore

    HAS_YAML = True
except ImportError:  # pragma: no cover
    _yaml = None
    HAS_YAML = False


WIKI_RELATED_START_MARKER = "<!-- oxenclaw:wiki:related:start -->"
WIKI_RELATED_END_MARKER = "<!-- oxenclaw:wiki:related:end -->"

_FRONTMATTER_RE = re.compile(r"^---\n([\s\S]*?)\n---\n?", re.MULTILINE)
_SLUG_NORMALIZE_RE = re.compile(r"[^a-z0-9-]+")
MAX_SLUG_BYTES = 240


def slugify_wiki_segment(name: str) -> str:
    """Filesystem-safe slug for `<vault>/<kind>/<slug>.md`.

    Keeps semantics close to openclaw's slugifier: lower, replace non-
    alnum-or-dash with `-`, collapse runs, strip edges, and append a
    short hash suffix when truncated to bound the filename length.
    """
    if not name.strip():
        return "untitled"
    s = name.strip().lower().replace("_", "-")
    s = _SLUG_NORMALIZE_RE.sub("-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if not s:
        return "untitled"
    encoded = s.encode("utf-8")
    if len(encoded) <= MAX_SLUG_BYTES:
        return s
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    head = encoded[: MAX_SLUG_BYTES - 9].decode("utf-8", errors="ignore").rstrip("-")
    return f"{head}.{digest}"


# ─── Frontmatter (de)serialization ──────────────────────────────────


def _dump_yaml(data: dict[str, Any]) -> str:
    if HAS_YAML and _yaml is not None:
        return _yaml.safe_dump(
            data, sort_keys=False, allow_unicode=True, default_flow_style=False
        ).rstrip("\n")
    # Minimal fallback dumper for the small shapes WikiPage uses.
    return _minimal_yaml_dump(data)


def _load_yaml(text: str) -> dict[str, Any]:
    if HAS_YAML and _yaml is not None:
        out = _yaml.safe_load(text)
        return out if isinstance(out, dict) else {}
    return _minimal_yaml_load(text)


def _minimal_yaml_dump(data: dict[str, Any], *, indent: int = 0) -> str:
    """Tiny YAML dumper covering scalar/list/dict-of-scalar nesting."""
    lines: list[str] = []
    pad = "  " * indent
    for key, value in data.items():
        if isinstance(value, list):
            if not value:
                lines.append(f"{pad}{key}: []")
            elif all(isinstance(v, (str, int, float, bool)) or v is None for v in value):
                rendered = ", ".join(_yaml_scalar(v) for v in value)
                lines.append(f"{pad}{key}: [{rendered}]")
            else:
                lines.append(f"{pad}{key}:")
                for item in value:
                    if isinstance(item, dict):
                        first = True
                        for k, v in item.items():
                            prefix = "- " if first else "  "
                            lines.append(f"{pad}{prefix}{k}: {_yaml_scalar(v)}")
                            first = False
                    else:
                        lines.append(f"{pad}- {_yaml_scalar(item)}")
        elif isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.append(_minimal_yaml_dump(value, indent=indent + 1))
        else:
            lines.append(f"{pad}{key}: {_yaml_scalar(value)}")
    return "\n".join(lines)


def _yaml_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if any(c in s for c in (":", "#", "\n", "[", "]", ",")):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _minimal_yaml_load(text: str) -> dict[str, Any]:
    """Best-effort: only parses simple `key: value` and `key: [a, b]` pairs.
    Pages produced by `_dump_yaml` round-trip; complex external YAML
    falls back to ignoring unknown lines."""
    out: dict[str, Any] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:
            out[key] = ""
            continue
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            out[key] = [v.strip().strip('"') for v in inner.split(",")] if inner else []
        elif value.lower() in ("true", "false"):
            out[key] = value.lower() == "true"
        elif value.lower() == "null":
            out[key] = None
        elif value.startswith('"') and value.endswith('"'):
            out[key] = value[1:-1]
        else:
            try:
                out[key] = int(value)
            except ValueError:
                try:
                    out[key] = float(value)
                except ValueError:
                    out[key] = value
    return out


# ─── WikiPage <-> markdown ──────────────────────────────────────────


def _dump_evidence(e: WikiEvidence) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if e.source_id:
        out["source_id"] = e.source_id
    if e.path:
        out["path"] = e.path
    if e.lines:
        out["lines"] = e.lines
    if e.note:
        out["note"] = e.note
    if e.weight is not None:
        out["weight"] = e.weight
    if e.updated_at is not None:
        out["updated_at"] = e.updated_at
    return out


def _dump_claim(c: WikiClaim) -> dict[str, Any]:
    out: dict[str, Any] = {"text": c.text}
    if c.evidence:
        out["evidence"] = [_dump_evidence(e) for e in c.evidence]
    if c.contested:
        out["contested"] = True
    if c.confidence is not None:
        out["confidence"] = c.confidence
    if c.asserted_at is not None:
        out["asserted_at"] = c.asserted_at
    if c.last_verified_at is not None:
        out["last_verified_at"] = c.last_verified_at
    if c.claim_id is not None:
        out["claim_id"] = c.claim_id
    return out


def render_wiki_markdown(page: WikiPage) -> str:
    """Serialise a WikiPage to markdown with YAML frontmatter."""
    fm: dict[str, Any] = {
        "kind": page.kind.value,
        "name": page.name,
        "slug": page.slug,
        "created_at": page.created_at,
        "updated_at": page.updated_at,
    }
    if page.aliases:
        fm["aliases"] = list(page.aliases)
    if page.tags:
        fm["tags"] = list(page.tags)
    if page.summary:
        fm["summary"] = page.summary
    if page.provenance_mode:
        fm["provenance_mode"] = page.provenance_mode
    if page.claims:
        fm["claims"] = [_dump_claim(c) for c in page.claims]

    related_block = ""
    if page.related:
        bullets = "\n".join(f"- [[{r}]]" for r in page.related)
        related_block = f"\n\n{WIKI_RELATED_START_MARKER}\n{bullets}\n{WIKI_RELATED_END_MARKER}"

    body = (page.body or "").rstrip()
    return f"---\n{_dump_yaml(fm)}\n---\n\n{body}{related_block}\n"


def parse_wiki_markdown(content: str) -> WikiPage:
    """Round-trip parser for content produced by `render_wiki_markdown`.

    Tolerant of pages edited by hand: missing fields use sensible
    defaults; unknown frontmatter keys are dropped.
    """
    match = _FRONTMATTER_RE.match(content)
    if match is None:
        raise ValueError("wiki page missing YAML frontmatter")
    fm_text = match.group(1)
    body_with_related = content[match.end() :].lstrip("\n")

    related: tuple[str, ...] = ()
    body = body_with_related
    related_match = re.search(
        re.escape(WIKI_RELATED_START_MARKER)
        + r"\n([\s\S]*?)\n"
        + re.escape(WIKI_RELATED_END_MARKER),
        body_with_related,
    )
    if related_match:
        bullets = related_match.group(1).strip().splitlines()
        related = tuple(
            ln.strip().lstrip("-").strip().strip("[]")
            for ln in bullets
            if ln.strip().startswith("- ")
        )
        body = body_with_related[: related_match.start()].rstrip() + "\n"

    fm = _load_yaml(fm_text)
    kind = parse_wiki_page_kind(str(fm.get("kind", "concept")))
    name = str(fm.get("name") or "Untitled")
    slug = str(fm.get("slug") or slugify_wiki_segment(name))
    aliases = tuple(fm.get("aliases") or ())
    tags = tuple(fm.get("tags") or ())
    summary = fm.get("summary") or None
    provenance_mode = fm.get("provenance_mode") or None
    created_at = float(fm.get("created_at") or time.time())
    updated_at = float(fm.get("updated_at") or created_at)

    raw_claims = fm.get("claims") or []
    claims: list[WikiClaim] = []
    if isinstance(raw_claims, list):
        for raw in raw_claims:
            if not isinstance(raw, dict):
                continue
            ev_list_raw = raw.get("evidence") or []
            evidence: list[WikiEvidence] = []
            if isinstance(ev_list_raw, list):
                for ev_raw in ev_list_raw:
                    if not isinstance(ev_raw, dict):
                        continue
                    evidence.append(
                        WikiEvidence(
                            source_id=ev_raw.get("source_id"),
                            path=ev_raw.get("path"),
                            lines=ev_raw.get("lines"),
                            note=ev_raw.get("note"),
                            weight=ev_raw.get("weight"),
                            updated_at=ev_raw.get("updated_at"),
                        )
                    )
            claims.append(
                WikiClaim(
                    text=str(raw.get("text") or ""),
                    evidence=tuple(evidence),
                    contested=bool(raw.get("contested", False)),
                    confidence=raw.get("confidence"),
                    asserted_at=raw.get("asserted_at"),
                    last_verified_at=raw.get("last_verified_at"),
                    claim_id=raw.get("claim_id") or None,
                )
            )

    return WikiPage(
        kind=kind,
        name=name,
        slug=slug,
        body=body.strip("\n"),
        aliases=aliases,
        tags=tags,
        related=related,
        claims=tuple(claims),
        summary=summary,
        provenance_mode=provenance_mode,
        created_at=created_at,
        updated_at=updated_at,
    )


__all__ = [
    "MAX_SLUG_BYTES",
    "WIKI_RELATED_END_MARKER",
    "WIKI_RELATED_START_MARKER",
    "parse_wiki_markdown",
    "render_wiki_markdown",
    "slugify_wiki_segment",
]
