"""`oxenclaw doctor` — aggregated health check.

Mirrors openclaw `src/flows/doctor-health.ts`. Probes every subsystem
the gateway needs at boot and reports per-area severity (ok / warn /
error). The wizard / TUI layer renders `DoctorReport` as a colourised
table; the CLI command lives in `oxenclaw.cli.flows_cmd`.

Probes are **best-effort**: each runs in isolation and exceptions are
caught + downgraded to a single `error` finding rather than aborting
the whole report. The user always gets a complete picture even when
one subsystem is broken.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from oxenclaw.config.paths import OxenclawPaths, default_paths

DoctorSeverity = Literal["ok", "warn", "error"]


@dataclass
class DoctorFinding:
    area: str
    severity: DoctorSeverity
    message: str
    detail: str | None = None

    def is_ok(self) -> bool:
        return self.severity == "ok"


@dataclass
class DoctorReport:
    findings: list[DoctorFinding] = field(default_factory=list)

    @property
    def errors(self) -> list[DoctorFinding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[DoctorFinding]:
        return [f for f in self.findings if f.severity == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(
        self, area: str, severity: DoctorSeverity, message: str, detail: str | None = None
    ) -> None:
        self.findings.append(
            DoctorFinding(area=area, severity=severity, message=message, detail=detail)
        )


def _probe_paths(paths: OxenclawPaths, report: DoctorReport) -> None:
    home = paths.home
    if not home.exists():
        report.add("paths", "warn", f"home directory missing: {home}", "first run will create it")
        return
    if not home.is_dir():
        report.add("paths", "error", f"home path is not a directory: {home}")
        return
    report.add("paths", "ok", f"home OK at {home}")


def _probe_config(paths: OxenclawPaths, report: DoctorReport) -> None:
    cfg = paths.config_file
    if not cfg.exists():
        report.add("config", "warn", "config.yaml missing", f"create one at {cfg}")
        return
    try:
        from oxenclaw.config import load_config

        load_config(paths)
        report.add("config", "ok", f"config.yaml parses cleanly ({cfg})")
    except Exception as exc:
        report.add("config", "error", "config.yaml parse failed", str(exc))


def _probe_credentials(paths: OxenclawPaths, report: DoctorReport) -> None:
    cred_dir = paths.credentials_dir
    if not cred_dir.exists():
        report.add("credentials", "warn", "no credentials directory", str(cred_dir))
        return
    count = sum(1 for p in cred_dir.rglob("*.json") if p.is_file())
    report.add(
        "credentials",
        "ok",
        f"{count} credential file(s) found",
        str(cred_dir),
    )


def _probe_mcp(paths: OxenclawPaths, report: DoctorReport) -> None:
    mcp = paths.mcp_config_file
    if not mcp.exists():
        report.add("mcp", "ok", "no mcp.json (no MCP servers configured)")
        return
    try:
        from oxenclaw.pi.mcp.loader import load_mcp_configs

        configs = load_mcp_configs(paths)
        report.add("mcp", "ok", f"{len(configs)} MCP server(s) configured")
    except Exception as exc:
        report.add("mcp", "error", "mcp.json parse failed", str(exc))


def _probe_embeddings(report: DoctorReport) -> None:
    """Surfaces embedding readiness for the configured backend.

    Two paths:

    - `OXENCLAW_EMBEDDER=llamacpp-direct` — check that the embedding
      GGUF and binary are reachable. Spawn is deferred (we don't want
      to fire up llama-server during a `doctor` run), but if the
      static prerequisites are wrong the operator can fix them
      without trial-and-error.
    - Default (Ollama) — reuse the existing endpoint probe from
      `config.preflight`, which actually hits `/v1/embeddings`.
    """
    import os as _os

    if _os.environ.get("OXENCLAW_EMBEDDER", "").strip() == "llamacpp-direct":
        gguf_raw = _os.environ.get("OXENCLAW_LLAMACPP_EMBED_GGUF", "").strip()
        if not gguf_raw:
            report.add(
                "embeddings",
                "warn",
                "OXENCLAW_EMBEDDER=llamacpp-direct but OXENCLAW_LLAMACPP_EMBED_GGUF unset",
                "run `oxenclaw setup llamacpp` to configure",
            )
            return
        from pathlib import Path as _Path

        p = _Path(_os.path.expanduser(gguf_raw))
        if not p.is_file():
            report.add(
                "embeddings",
                "warn",
                f"embedding GGUF unreachable: {p}",
                "fix the path or re-run `oxenclaw setup llamacpp`",
            )
            return
        try:
            from oxenclaw.pi.llamacpp_server.manager import find_llama_server_binary

            find_llama_server_binary()
        except Exception as exc:
            report.add("embeddings", "warn", "llama-server binary not discoverable", str(exc))
            return
        size_mb = p.stat().st_size / (1024 * 1024)
        report.add(
            "embeddings",
            "ok",
            "llamacpp-direct embedder ready",
            f"gguf={p} ({size_mb:.0f} MiB) — server spawns on first request",
        )
        return

    try:
        from oxenclaw.config.preflight import PreflightReport, _check_embedding_endpoint

        sub = PreflightReport()
        _check_embedding_endpoint(sub)
    except Exception as exc:
        report.add("embeddings", "error", "embedding probe crashed", str(exc))
        return
    if not sub.findings:
        report.add("embeddings", "ok", "embedding endpoint reachable")
        return
    for f in sub.findings:
        sev: DoctorSeverity = "warn" if f.severity == "warning" else "error"
        report.add("embeddings", sev, f.message)


def _probe_context_engines(report: DoctorReport) -> None:
    try:
        # Call the registration helper directly rather than the
        # `ensure_*_initialized()` guard — that guard is process-wide
        # and can be flipped to `True` by tests / earlier code while
        # the registry itself sits empty (e.g. a `_reset_for_tests()`
        # call followed by a fresh probe). Direct registration is
        # idempotent under same-owner refresh, so this is safe.
        from oxenclaw.pi.context_engine.legacy import register_legacy_context_engine
        from oxenclaw.pi.context_engine.registry import list_slots

        register_legacy_context_engine()
        slots = list_slots()
    except Exception as exc:
        report.add("context-engine", "error", "registry probe crashed", str(exc))
        return
    if not slots:
        report.add("context-engine", "warn", "no context engines registered")
        return
    report.add(
        "context-engine",
        "ok",
        f"{len(slots)} engine(s) registered",
        ", ".join(slots),
    )


def _probe_providers(report: DoctorReport) -> None:
    try:
        import oxenclaw.pi.providers  # noqa: F401  registers wrappers
        from oxenclaw.agents.factory import CATALOG_PROVIDERS
        from oxenclaw.pi.streaming import _PROVIDER_STREAMS  # type: ignore[attr-defined]
    except Exception as exc:
        report.add("providers", "error", "provider import failed", str(exc))
        return
    catalog = set(CATALOG_PROVIDERS)
    registered = set(_PROVIDER_STREAMS.keys())
    # Direction matters: an "advertised but not wired" id breaks the
    # CLI (factory routes a real user request to a missing wrapper).
    # The reverse — extra wrappers without a matching catalog id — is
    # legitimate: third-party plugins and test fixtures register custom
    # streams that are intentionally not in the public catalog.
    missing = catalog - registered
    if missing:
        report.add(
            "providers",
            "error",
            "catalog provider(s) advertised but no stream wrapper registered",
            f"missing: {sorted(missing)}",
        )
        return
    extra = registered - catalog
    detail = f"{len(catalog)} catalog providers wired"
    if extra:
        detail += f" (+{len(extra)} non-catalog stream(s) registered: {sorted(extra)})"
    report.add("providers", "ok", detail)


def _probe_llamacpp_direct(report: DoctorReport) -> None:
    """Surface the two manual prerequisites for `--provider llamacpp-direct`.

    Both `llama-server` (binary) and `$OXENCLAW_LLAMACPP_GGUF` (weights)
    must be reachable before the managed-server path can boot. We don't
    fail if they aren't — Ollama is the documented fallback — but we
    point users at the one-shot wizard so they don't have to assemble
    the recipe by hand.
    """
    import os as _os

    try:
        from oxenclaw.pi.llamacpp_server.manager import (
            LlamaCppServerError,
            find_llama_server_binary,
        )
    except Exception as exc:
        report.add("llamacpp-direct", "warn", "manager import failed", str(exc))
        return

    binary_path: str | None = None
    try:
        binary_path = str(find_llama_server_binary())
    except LlamaCppServerError:
        binary_path = None

    gguf_raw = _os.environ.get("OXENCLAW_LLAMACPP_GGUF", "").strip()
    gguf_ok = False
    gguf_detail = ""
    if gguf_raw:
        from pathlib import Path as _Path

        p = _Path(_os.path.expanduser(gguf_raw))
        if p.is_file():
            size_mb = p.stat().st_size / (1024 * 1024)
            gguf_ok = True
            gguf_detail = f"{p} ({size_mb:.0f} MiB)"
        else:
            gguf_detail = f"{p} (not found)"

    if binary_path and gguf_ok:
        # Quick "does this binary actually run on this host?" check —
        # SYCL/ROCm/Vulkan prebuilt assets dropped on a CUDA-only box
        # exit code 127 at first spawn because the dynamic linker can't
        # find their backend's runtime (`libsvml.so`, `libamdhip64.so`,
        # …). Catch that here instead of mid-chat.
        import subprocess as _sp
        from pathlib import Path as _Path

        binary_dir = str(_Path(binary_path).resolve().parent)
        existing_ld = _os.environ.get("LD_LIBRARY_PATH", "")
        new_ld = f"{binary_dir}:{existing_ld}" if existing_ld else binary_dir
        try:
            r = _sp.run(
                [binary_path, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                env={**_os.environ, "LD_LIBRARY_PATH": new_ld},
            )
        except (FileNotFoundError, _sp.TimeoutExpired) as exc:
            report.add(
                "llamacpp-direct",
                "warn",
                "binary smoke spawn failed",
                f"{binary_path}: {exc}",
            )
            return
        if r.returncode != 0:
            tail = ((r.stderr or r.stdout) or "")[-400:].strip()
            report.add(
                "llamacpp-direct",
                "warn",
                f"binary won't run on this host (rc={r.returncode}) — likely "
                "wrong-arch prebuilt (SYCL/ROCm asset on CUDA box). "
                "Re-run `oxenclaw setup llamacpp` and pick build-from-source.",
                f"{binary_path}\n{tail}",
            )
            return
        report.add(
            "llamacpp-direct",
            "ok",
            "ready",
            f"binary={binary_path}; gguf={gguf_detail}",
        )
        return

    missing: list[str] = []
    if not binary_path:
        missing.append("llama-server binary (set $OXENCLAW_LLAMACPP_BIN or put on $PATH)")
    if not gguf_raw:
        missing.append("$OXENCLAW_LLAMACPP_GGUF (path to a downloaded GGUF)")
    elif not gguf_ok:
        missing.append(f"GGUF unreachable: {gguf_detail}")

    report.add(
        "llamacpp-direct",
        "warn",
        "not configured — `oxenclaw setup llamacpp` will set this up in one shot",
        "; ".join(missing),
    )


def _probe_plugins(report: DoctorReport) -> None:
    try:
        from oxenclaw.plugins import discover_plugins

        plugins = discover_plugins()
    except Exception as exc:
        report.add("plugins", "warn", "plugin discovery failed", str(exc))
        return
    report.add(
        "plugins",
        "ok",
        f"{len(plugins)} plugin(s) discovered via entry-points",
    )


def _probe_isolation(report: DoctorReport) -> None:
    try:
        from oxenclaw.plugin_sdk.runtime_env import describe_platform, is_wsl

        platform = describe_platform()
    except Exception as exc:
        report.add("isolation", "warn", "platform probe failed", str(exc))
        return
    detail = platform
    try:
        import shutil

        bwrap = shutil.which("bwrap")
        firejail = shutil.which("firejail")
        backends = []
        if bwrap:
            backends.append(f"bwrap={bwrap}")
        if firejail:
            backends.append(f"firejail={firejail}")
        if backends:
            detail = f"{platform}; {' '.join(backends)}"
    except Exception:
        pass
    sev: DoctorSeverity = "ok"
    msg = "isolation backend available"
    if "bwrap" not in detail and "firejail" not in detail:
        sev = "warn"
        msg = "no bwrap/firejail backend — sandboxed tools will fall back to subprocess"
    if is_wsl():
        msg = f"{msg} (WSL2 — see docs/INSTALL_WSL.md)"
    report.add("isolation", sev, msg, detail)


def _probe_memory_store(paths: OxenclawPaths, report: DoctorReport) -> None:
    memory_dir = paths.home / "memory"
    if not memory_dir.exists():
        report.add("memory", "ok", "memory store not yet initialised", str(memory_dir))
        return
    try:
        total = sum(p.stat().st_size for p in memory_dir.rglob("*") if p.is_file())
        size_mb = total / (1024 * 1024)
        report.add("memory", "ok", f"memory store {size_mb:.1f} MiB", str(memory_dir))
    except Exception as exc:
        report.add("memory", "warn", "memory store stat failed", str(exc))


def _probe_sessions(paths: OxenclawPaths, report: DoctorReport) -> None:
    if not paths.agents_dir.exists():
        report.add(
            "sessions",
            "ok",
            "no per-agent session stores yet",
            str(paths.agents_dir),
        )
        return
    try:
        agent_count = sum(1 for p in paths.agents_dir.iterdir() if p.is_dir())
        report.add(
            "sessions",
            "ok",
            f"{agent_count} agent dir(s) under {paths.agents_dir}",
        )
    except Exception as exc:
        report.add("sessions", "warn", "session store stat failed", str(exc))


def run_doctor(
    paths: OxenclawPaths | None = None,
    *,
    probe_embeddings: bool = True,
) -> DoctorReport:
    """Run every health probe and return the aggregated `DoctorReport`.

    Set `probe_embeddings=False` for offline / unit-test contexts where
    a 5-second urllib timeout against a non-existent endpoint isn't
    desired.
    """
    resolved = paths or default_paths()
    report = DoctorReport()
    _probe_paths(resolved, report)
    _probe_config(resolved, report)
    _probe_credentials(resolved, report)
    _probe_mcp(resolved, report)
    _probe_providers(report)
    _probe_llamacpp_direct(report)
    _probe_context_engines(report)
    _probe_plugins(report)
    _probe_isolation(report)
    _probe_memory_store(resolved, report)
    _probe_sessions(resolved, report)
    if probe_embeddings:
        _probe_embeddings(report)
    return report


__all__ = [
    "DoctorFinding",
    "DoctorReport",
    "DoctorSeverity",
    "run_doctor",
]
