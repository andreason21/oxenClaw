"""`llama-server` process supervisor.

Owns the lifecycle of a single, long-lived `llama-server` child process:
discovery of the binary, port allocation, spawn with sane defaults,
`/health` readiness probe, stdout drain, graceful + forced shutdown,
and orphan cleanup at process start / exit.

Design choices borrowed from unsloth-studio's `LlamaCppBackend`
(`studio/backend/core/inference/llama_cpp.py`):

- Persistent server (per-GGUF). The boot cost (mmap + VRAM upload) is
  paid once. Switching GGUFs kills and restarts; same-GGUF reload is a
  no-op.
- `--flash-attn on`, `--jinja`, `--no-context-shift`, `-ngl 999`
  defaults. These are the flags that make the same model noticeably
  faster than going through Ollama.
- A daemon stdout drain thread prevents the 4-KB pipe-buffer deadlock
  on Windows and gives us recent server logs for crash diagnostics.
- `atexit` cleanup + a best-effort orphan kill at start so a crashed
  parent doesn't leave a zombie holding VRAM.

The manager is intentionally synchronous + thread-based: process
supervision lives outside the asyncio event loop so a stuck event loop
can't strand the child. The provider streamer can call `ensure_loaded`
from sync context and only switches to async for the HTTP stream.
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.llamacpp_server")

# ─── Binary discovery ─────────────────────────────────────────────────


_BINARY_ENV_VARS: tuple[str, ...] = (
    "OXENCLAW_LLAMACPP_BIN",
    "LLAMA_SERVER_PATH",
)


def _candidate_install_dirs() -> list[Path]:
    """Common install locations for a prebuilt `llama-server` binary."""
    home = Path.home()
    return [
        home / ".oxenclaw" / "llama.cpp",
        home / ".oxenclaw" / "llama.cpp" / "build" / "bin",
        Path("/usr/local/bin"),
        Path("/opt/llama.cpp/bin"),
    ]


def find_llama_server_binary() -> Path:
    """Locate the `llama-server` binary.

    Search order:
      1. `OXENCLAW_LLAMACPP_BIN` / `LLAMA_SERVER_PATH` env vars
         (full path).
      2. `shutil.which("llama-server")` — anything on PATH.
      3. Common install dirs (`~/.oxenclaw/llama.cpp`,
         `/usr/local/bin`, `/opt/llama.cpp/bin`).

    Raises `LlamaCppServerError` if nothing is found — installation
    instructions are intentionally short because the user explicitly
    opted into "I downloaded llama.cpp myself, just point at it".
    """
    for env in _BINARY_ENV_VARS:
        raw = os.environ.get(env, "").strip()
        if raw:
            p = Path(os.path.expanduser(raw))
            if p.is_file() and os.access(p, os.X_OK):
                return p
            raise LlamaCppServerError(f"${env}={raw!r} is not an executable file")

    on_path = shutil.which("llama-server")
    if on_path:
        return Path(on_path)

    for d in _candidate_install_dirs():
        for name in ("llama-server", "llama-server.exe"):
            p = d / name
            if p.is_file() and os.access(p, os.X_OK):
                return p

    raise LlamaCppServerError(
        "llama-server binary not found. Install llama.cpp and either "
        "place `llama-server` on PATH or set "
        "$OXENCLAW_LLAMACPP_BIN=/path/to/llama-server."
    )


# ─── Helpers ──────────────────────────────────────────────────────────


def find_free_port() -> int:
    """Bind to port 0 to let the kernel pick a free localhost port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaCppServerError(RuntimeError):
    """Raised when spawn, health-check, or shutdown fails."""


# ─── Spec + default flags ─────────────────────────────────────────────


@dataclass(frozen=True)
class LlamaCppServerSpec:
    """Knobs for a single `llama-server` instance.

    `gguf_path` is the only required field — everything else has a
    sane default that mirrors unsloth-studio's "fast" preset. Operators
    who need to micro-tune pass `extra_args` (raw flag list, appended
    after defaults so they win on conflict).
    """

    gguf_path: Path
    # 65536 chosen to match the bundled assistant agent's typical
    # system-prompt + memory + manifest envelope (~33K tokens) plus
    # a turn or two of conversation headroom. 16384 / 32768 both proved
    # insufficient and triggered context_overflow on populated memory
    # installs.
    n_ctx: int = 65536
    n_gpu_layers: int = 999  # all layers; llama.cpp clamps to model max
    n_threads: int = -1  # -1 → llama.cpp picks (physical core count)
    n_parallel: int = 1
    n_batch: int | None = None
    api_key: str | None = None
    chat_template: str | None = None  # path to .jinja, optional
    mmproj_path: Path | None = None  # multimodal projector, optional
    flash_attn: bool = True
    no_context_shift: bool = True
    use_jinja: bool = True
    # Embedding mode: emit `--embedding` and pick the right pooling for
    # the model. When True, chat-only flags (`--no-context-shift`,
    # `--jinja`) are auto-dropped because they're meaningless for the
    # embedding endpoint and some models reject them outright.
    embedding: bool = False
    pooling: str | None = None  # mean / cls / last / rank — default lets llama.cpp pick
    extra_args: tuple[str, ...] = ()
    env_overrides: dict[str, str] = field(default_factory=dict)

    def cache_key(self) -> tuple[Any, ...]:
        """Identity for "is the running server already serving this?".
        Two specs that produce the same `cache_key` are considered
        equivalent and don't trigger a restart."""
        return (
            str(self.gguf_path),
            self.n_ctx,
            self.n_gpu_layers,
            self.n_threads,
            self.n_parallel,
            self.n_batch,
            self.chat_template,
            None if self.mmproj_path is None else str(self.mmproj_path),
            self.flash_attn,
            self.no_context_shift,
            self.use_jinja,
            self.embedding,
            self.pooling,
            self.extra_args,
        )


def _build_command(binary: Path, spec: LlamaCppServerSpec, port: int) -> list[str]:
    """Assemble the full `llama-server` argv for `spec`.

    The flags here are the "fast preset". Comments inline cite the
    unsloth-studio file:line they were lifted from.
    """
    cmd: list[str] = [
        str(binary),
        "-m",
        str(spec.gguf_path),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "-c",
        str(spec.n_ctx),
        "--parallel",
        str(spec.n_parallel),
        "-ngl",
        str(spec.n_gpu_layers),
    ]
    if spec.n_threads and spec.n_threads > 0:
        cmd += ["--threads", str(spec.n_threads)]
    if spec.n_batch:
        cmd += ["-b", str(spec.n_batch)]
    if spec.flash_attn:
        # studio/backend/core/inference/llama_cpp.py:1609-1610 — forced on.
        cmd += ["--flash-attn", "on"]
    if spec.embedding:
        # Embedding endpoint mode — implies a different model and a
        # different request shape. Chat-only flags are dropped.
        cmd += ["--embedding"]
        if spec.pooling:
            cmd += ["--pooling", spec.pooling]
    else:
        if spec.no_context_shift:
            # llama_cpp.py:1612 — fail fast at ctx limit instead of slow rotate.
            cmd += ["--no-context-shift"]
        if spec.use_jinja:
            # llama_cpp.py:1627 — use the GGUF's own chat template.
            cmd += ["--jinja"]
        if spec.chat_template:
            cmd += ["--chat-template-file", str(spec.chat_template)]
        if spec.mmproj_path is not None:
            cmd += ["--mmproj", str(spec.mmproj_path)]
    if spec.api_key:
        cmd += ["--api-key", spec.api_key]
    cmd += list(spec.extra_args)
    return cmd


# ─── Manager ──────────────────────────────────────────────────────────


_HEALTH_TIMEOUT_DEFAULT_S = 600.0
_HEALTH_POLL_INTERVAL_S = 0.5
_SHUTDOWN_GRACE_S = 5.0
_FAIL_CACHE_TTL_S = 60.0  # how long to remember a bad spawn before retrying


class LlamaCppServer:
    """Single-instance `llama-server` supervisor.

    Thread-safe: `ensure_loaded` and `unload` hold a reentrant lock so
    concurrent agents don't race on spawn/kill. The supervisor itself
    is sync; the provider does its own asyncio HTTP streaming once
    `ensure_loaded` returns the base URL.
    """

    def __init__(self, *, binary: Path | None = None) -> None:
        self._binary_override = binary
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._port: int | None = None
        self._spec: LlamaCppServerSpec | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stdout_tail: list[str] = []  # last N lines, for crash dumps
        self._tail_max = 200
        self._cleanup_registered = False
        # Failure cache: keyed by `spec.cache_key()` → (timestamp, error
        # message). Once a spawn fails, the same spec is rejected fast
        # for `_FAIL_CACHE_TTL_S` seconds so a single broken config
        # doesn't block every memory.search and chat.send for ~10
        # minutes per call (the original bug shape: 4 retries × 600 s
        # health timeout). Cleared on `unload()`.
        self._spawn_fail_cache: dict[tuple, tuple[float, str]] = {}

    # — public API ────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """True if a child process is alive and was readiness-checked."""
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    @property
    def base_url(self) -> str | None:
        """OpenAI-compat base URL (`http://127.0.0.1:<port>/v1`) or None."""
        with self._lock:
            if self._port is None or not self.is_running:
                return None
            return f"http://127.0.0.1:{self._port}/v1"

    def ensure_loaded(
        self, spec: LlamaCppServerSpec, *, health_timeout_s: float | None = None
    ) -> str:
        """Make sure a server matching `spec` is running. Returns base URL.

        - If a server with the same `cache_key` is already running, no-op.
        - If a different server is running, kill it first, then spawn.
        - On failure, the previous server is left in whatever state the
          kill landed (unload best-effort) and an error is raised.
        """
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                if self._spec is not None and self._spec.cache_key() == spec.cache_key():
                    assert self._port is not None
                    return f"http://127.0.0.1:{self._port}/v1"
                logger.info(
                    "llama-server: spec changed, restarting (was=%s, new=%s)",
                    self._spec.gguf_path if self._spec else None,
                    spec.gguf_path,
                )
                self._kill_locked()

            # Reject fast if this exact spec just failed — protects
            # callers (memory.search, chat prelude, …) from a 4-deep
            # cascade of 600-second health timeouts on a misconfigured
            # GGUF or unsupported flag.
            cache_key = spec.cache_key()
            now = time.monotonic()
            cached = self._spawn_fail_cache.get(cache_key)
            if cached is not None and (now - cached[0]) < _FAIL_CACHE_TTL_S:
                age = int(now - cached[0])
                raise LlamaCppServerError(
                    f"recent spawn failure cached ({age}s ago, retry after "
                    f"{int(_FAIL_CACHE_TTL_S - (now - cached[0]))}s, "
                    f"or call .unload() to force-clear): {cached[1]}"
                )

            try:
                return self._spawn_locked(
                    spec, timeout_s=health_timeout_s or _HEALTH_TIMEOUT_DEFAULT_S
                )
            except LlamaCppServerError as exc:
                self._spawn_fail_cache[cache_key] = (now, str(exc))
                raise

    def unload(self) -> None:
        """Kill the child process if any. Idempotent. Also clears the
        spawn-failure cache so a manual `unload()` doubles as "I fixed
        the config, let me retry now" without waiting out the TTL.
        """
        with self._lock:
            self._kill_locked()
            self._spawn_fail_cache.clear()

    def stdout_tail(self, n: int = 50) -> list[str]:
        """Last `n` stdout/stderr lines from the child. For crash dumps."""
        with self._lock:
            return list(self._stdout_tail[-n:])

    # — internals ─────────────────────────────────────────────────────

    def _binary(self) -> Path:
        if self._binary_override is not None:
            return self._binary_override
        return find_llama_server_binary()

    def _spawn_locked(self, spec: LlamaCppServerSpec, *, timeout_s: float) -> str:
        if not spec.gguf_path.is_file():
            raise LlamaCppServerError(
                f"GGUF not found: {spec.gguf_path}. Download the file "
                f"manually and pass model.extra['gguf_path'] = '<path>' "
                f"or set $OXENCLAW_LLAMACPP_GGUF."
            )

        binary = self._binary()
        port = find_free_port()
        cmd = _build_command(binary, spec, port)
        env = os.environ.copy()
        # Prebuilt llama.cpp tarballs ship `libllama.so` / `libggml-*.so`
        # next to the `llama-server` executable. Without putting that
        # directory on the dynamic loader's search path, the child dies
        # at startup with "error while loading shared libraries:
        # libllama-common.so.0: cannot open shared object file" —
        # silently, into stdout we never get because the process exited
        # before the `--help` line. Prepend it for both Linux/glibc and
        # macOS dyld so prebuilt and source builds both Just Work.
        binary_dir = str(binary.resolve().parent)
        for key in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
            existing = env.get(key, "")
            env[key] = f"{binary_dir}:{existing}" if existing else binary_dir
        env.update(spec.env_overrides)

        logger.info("llama-server: spawn %s (port=%d)", " ".join(cmd), port)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                close_fds=True,
            )
        except OSError as exc:
            raise LlamaCppServerError(f"failed to spawn llama-server: {exc}") from exc

        self._proc = proc
        self._port = port
        self._spec = spec
        self._stdout_tail = []
        self._stdout_thread = threading.Thread(
            target=self._drain_stdout,
            args=(proc,),
            daemon=True,
            name="llama-server-stdout",
        )
        self._stdout_thread.start()

        if not self._cleanup_registered:
            atexit.register(self._atexit_cleanup)
            self._cleanup_registered = True

        try:
            self._wait_for_health(proc, port, timeout_s=timeout_s)
        except LlamaCppServerError as health_exc:
            # Health probe failed — kill child to avoid leaking VRAM and
            # surface what the server actually said in stdout to make
            # the failure debuggable. Race-safe tail capture: join the
            # drain thread (briefly) so an early-exiting child has time
            # to flush whatever it wrote before we read `_stdout_tail`.
            reason = str(health_exc) or "health probe failed"
            self._kill_locked()
            if self._stdout_thread is not None:
                self._stdout_thread.join(timeout=1.5)
            tail_lines = self._stdout_tail[-30:]
            tail = (
                "\n".join(tail_lines)
                if tail_lines
                else (
                    "(empty — child crashed before printing; try the spawn "
                    "manually to see the real stderr: \n  "
                    f"{' '.join(cmd)})"
                )
            )
            raise LlamaCppServerError(f"{reason}. Last stdout:\n{tail}") from None

        return f"http://127.0.0.1:{port}/v1"

    def _wait_for_health(
        self, proc: subprocess.Popen[bytes], port: int, *, timeout_s: float
    ) -> None:
        url = f"http://127.0.0.1:{port}/health"
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            rc = proc.poll()
            if rc is not None:
                # Hint at the most common cause when stdout is empty:
                # the child rejected its argv (unsupported flag) and
                # printed only to stderr we'd already merged into
                # stdout. Either way, the spawn is doomed; bail.
                raise LlamaCppServerError(f"llama-server exited early with code {rc}")
            try:
                with urllib.request.urlopen(url, timeout=1.5) as resp:
                    if 200 <= resp.status < 500:
                        # llama-server returns 503 while loading — treat
                        # only 2xx as ready. 4xx happens transiently
                        # before the route is registered; keep polling.
                        if 200 <= resp.status < 300:
                            return
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                pass
            time.sleep(_HEALTH_POLL_INTERVAL_S)
        raise LlamaCppServerError("health probe timed out")

    def _drain_stdout(self, proc: subprocess.Popen[bytes]) -> None:
        """Daemon thread: read child stdout to keep the pipe drained.

        Without this, llama-server hangs after a few hundred log lines
        when the parent stops reading. We also keep the last N lines
        for crash diagnostics — the user gets concrete server output
        on a spawn failure instead of a generic timeout.
        """
        if proc.stdout is None:
            return
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                with self._lock:
                    self._stdout_tail.append(line)
                    if len(self._stdout_tail) > self._tail_max:
                        del self._stdout_tail[0 : len(self._stdout_tail) - self._tail_max]
        except (OSError, ValueError):
            return

    def _kill_locked(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=_SHUTDOWN_GRACE_S)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except (OSError, ProcessLookupError):
                    pass
                try:
                    proc.wait(timeout=_SHUTDOWN_GRACE_S)
                except subprocess.TimeoutExpired:
                    logger.warning("llama-server (pid=%s) ignored SIGKILL", proc.pid)
        self._proc = None
        self._port = None
        self._spec = None

    def _atexit_cleanup(self) -> None:
        try:
            with self._lock:
                self._kill_locked()
        except Exception:  # pragma: no cover — atexit must not raise
            pass


# ─── Process-global singleton ─────────────────────────────────────────


_default_server_lock = threading.Lock()
_default_server: LlamaCppServer | None = None
_embedding_server_lock = threading.Lock()
_embedding_server: LlamaCppServer | None = None


def get_default_server() -> LlamaCppServer:
    """Process-wide chat-side `LlamaCppServer` (lazy-init)."""
    global _default_server
    with _default_server_lock:
        if _default_server is None:
            _default_server = LlamaCppServer()
        return _default_server


def get_embedding_server() -> LlamaCppServer:
    """Process-wide embedding-side `LlamaCppServer` (lazy-init).

    Separate from `get_default_server()` because chat and embedding GGUFs
    are different models entirely — running them under the same server
    would force a kill-and-restart on every chat↔embedding switch. With
    two managers each model stays warm and they fight only for VRAM.
    """
    global _embedding_server
    with _embedding_server_lock:
        if _embedding_server is None:
            _embedding_server = LlamaCppServer()
        return _embedding_server


__all__ = [
    "LlamaCppServer",
    "LlamaCppServerError",
    "LlamaCppServerSpec",
    "find_free_port",
    "find_llama_server_binary",
    "get_default_server",
    "get_embedding_server",
]


# Pin SIGINT default so a Ctrl-C against the parent doesn't double-deliver
# to the child via the shared process group on POSIX. We only adjust if
# nothing else has set a handler; respects user-provided handlers.
if hasattr(signal, "SIGINT") and threading.current_thread() is threading.main_thread():
    try:
        existing = signal.getsignal(signal.SIGINT)
        if existing in (signal.SIG_DFL, None):
            signal.signal(signal.SIGINT, signal.default_int_handler)
    except (ValueError, OSError):
        pass
