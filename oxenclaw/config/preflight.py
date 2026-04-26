"""Aggregated startup-time config validation.

`run_preflight()` validates every config surface that oxenClaw consumes
on boot: `config.yaml`, `mcp.json`, env-var references, and credential
files referenced by the config. The intent is **fail-fast** at gateway
start so a malformed deployment doesn't get partway up before failing
in a confusing place.

Two modes:

- **strict** — any error fails preflight. Used by default in
  `gateway start`.
- **lenient** — errors collected and returned, caller decides. Used by
  the `oxenclaw config validate` command which prints them all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from oxenclaw.config import ConfigError, load_config
from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.pi.mcp.loader import load_mcp_configs

# Same shape `config/env_subst.py` consumes.
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


@dataclass
class PreflightFinding:
    severity: str  # "error" | "warning"
    source: str
    message: str

    def format(self) -> str:
        return f"[{self.severity}] {self.source}: {self.message}"


@dataclass
class PreflightReport:
    findings: list[PreflightFinding] = field(default_factory=list)

    @property
    def errors(self) -> list[PreflightFinding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[PreflightFinding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, severity: str, source: str, message: str) -> None:
        self.findings.append(PreflightFinding(severity=severity, source=source, message=message))


def _collect_env_refs(value: object) -> set[str]:
    """Walk a JSON-like structure and return every `$VAR` / `${VAR}` name."""
    refs: set[str] = set()
    if isinstance(value, str):
        for match in _ENV_REF_RE.finditer(value):
            refs.add(match.group(1) or match.group(2))
    elif isinstance(value, dict):
        for v in value.values():
            refs |= _collect_env_refs(v)
    elif isinstance(value, list):
        for v in value:
            refs |= _collect_env_refs(v)
    return refs


def _check_config_yaml(paths: OxenclawPaths, report: PreflightReport) -> object | None:
    src = str(paths.config_file)
    try:
        cfg = load_config(paths)
    except ConfigError as exc:
        report.add("error", src, str(exc))
        return None
    return cfg


def _check_mcp_json(paths: OxenclawPaths, report: PreflightReport) -> None:
    target = paths.mcp_config_file
    if not target.exists():
        return
    src = str(target)
    configs, diagnostics = load_mcp_configs(paths)
    for name, reason in diagnostics:
        report.add("error", f"{src}::{name}", reason)
    if configs:
        report.add(
            "warning",
            src,
            f"{len(configs)} MCP server(s) configured — they will be "
            "connected on startup (check pool.failures for runtime failures)",
        )


def _check_credentials_dir(paths: OxenclawPaths, report: PreflightReport) -> None:
    cred_dir = paths.credentials_dir
    if not cred_dir.exists():
        return  # no credentials yet — fine for fresh installs
    for entry in cred_dir.rglob("*.json"):
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError as exc:
            report.add("error", str(entry), f"unreadable: {exc}")
            continue
        if not text.strip():
            report.add("warning", str(entry), "empty credentials file")
            continue
        try:
            import json as _json

            _json.loads(text)
        except Exception as exc:
            report.add("error", str(entry), f"malformed JSON: {exc}")


def _check_env_refs_in_files(paths: OxenclawPaths, report: PreflightReport) -> None:
    """Surface env-var references that won't expand (var not set).

    Currently checks `mcp.json` (the most likely place to embed
    `${SECRET}`). `config.yaml` substitution already happens at load
    time and is reflected in the cfg object — env validation there is
    handled by `_check_config_yaml`'s ConfigError surface.
    """
    import json as _json
    import os

    target = paths.mcp_config_file
    if not target.exists():
        return
    try:
        raw = _json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return  # the json error path is already handled by _check_mcp_json
    refs = _collect_env_refs(raw)
    missing = sorted(name for name in refs if name not in os.environ)
    if missing:
        joined = ", ".join(missing)
        report.add(
            "warning",
            str(target),
            f"env reference(s) not set in environment: {joined} "
            "(literals will be left in place, which is likely a misconfig)",
        )


def _check_embedding_endpoint(report: PreflightReport) -> None:
    """Probe the embedding endpoint with the configured model.

    Reports a WARNING (not error) so the gateway still boots — memory
    features simply won't work until the operator pulls the model or
    points to a different endpoint. Uses stdlib urllib with a short
    timeout so we don't drag aiohttp into the sync preflight path.
    """
    import json as _json
    import os
    import urllib.error
    import urllib.request

    from oxenclaw.memory.embeddings import (
        DEFAULT_EMBED_BASE_URL,
        DEFAULT_EMBED_MODEL,
    )

    base_url = os.environ.get("OXENCLAW_EMBED_BASE_URL", DEFAULT_EMBED_BASE_URL).rstrip("/")
    model = os.environ.get("OXENCLAW_EMBED_MODEL", DEFAULT_EMBED_MODEL)
    url = f"{base_url}/embeddings"
    payload = _json.dumps({"model": model, "input": "preflight"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    api_key = os.environ.get("OXENCLAW_EMBED_API_KEY")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            if resp.status >= 400:
                report.add(
                    "warning",
                    "embeddings",
                    f"{url} returned HTTP {resp.status} — memory features will be unavailable",
                )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            report.add(
                "warning",
                "embeddings",
                f"{url} model '{model}' not found (HTTP 404). "
                f"Run `ollama pull {model}` on the host, or set "
                f"OXENCLAW_EMBED_MODEL to a model you've already pulled. "
                f"Memory features will be unavailable until this is fixed.",
            )
        else:
            report.add(
                "warning",
                "embeddings",
                f"{url} returned HTTP {exc.code}: {exc.reason}",
            )
    except urllib.error.URLError as exc:
        report.add(
            "warning",
            "embeddings",
            f"{url} unreachable: {exc.reason}. "
            f"Set OXENCLAW_EMBED_BASE_URL if your embedding service "
            f"isn't on {base_url}, or start Ollama with "
            f"OLLAMA_HOST=0.0.0.0:11434.",
        )
    except Exception as exc:
        report.add("warning", "embeddings", f"{url} probe failed: {exc}")


def run_preflight(
    paths: OxenclawPaths | None = None,
    *,
    probe_embeddings: bool = True,
) -> PreflightReport:
    """Run every startup-time check and return an aggregated report.

    Set ``probe_embeddings=False`` for offline / unit-test contexts where
    the embedding endpoint isn't expected to be reachable.
    """
    resolved = paths or default_paths()
    report = PreflightReport()
    _check_config_yaml(resolved, report)
    _check_mcp_json(resolved, report)
    _check_credentials_dir(resolved, report)
    _check_env_refs_in_files(resolved, report)
    if probe_embeddings:
        _check_embedding_endpoint(report)
    return report


__all__ = [
    "PreflightFinding",
    "PreflightReport",
    "run_preflight",
]
