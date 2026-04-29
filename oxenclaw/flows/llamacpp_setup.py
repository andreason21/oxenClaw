"""One-shot setup wizard for the `llamacpp-direct` provider.

Coordinates the three manual steps (binary, GGUF, env persistence)
into a single interactive flow so users don't have to assemble the
recipe from `docs/LLAMACPP_DIRECT.md` by hand.

Design choices that matter for testability:

- The wizard is a class with **injectable IO**: a `Prompter` Protocol
  for stdin and a `WizardIO` Protocol for filesystem / network /
  subprocess side effects. The CLI provides typer-backed concrete
  implementations; tests provide stubs that record calls.
- Each step (`step_binary`, `step_gguf`, `step_persist`,
  `step_smoke_test`) is independently runnable so individual paths
  can be exercised in tests without driving the whole flow.
- The persistence target is `~/.oxenclaw/env` (a shell-sourced
  key=value file) rather than `~/.bashrc` / `~/.zshrc` directly.
  Modifying the user's rc is gated on an explicit confirm.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tarfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# ─── IO contracts ─────────────────────────────────────────────────────


class Prompter(Protocol):
    """stdin abstraction. Same shape as `flows.model_picker.Prompter`."""

    def select(self, message: str, choices: list[str], *, default: str | None = None) -> str: ...

    def text(self, message: str, *, default: str | None = None, secret: bool = False) -> str: ...

    def confirm(self, message: str, *, default: bool = True) -> bool: ...


class WizardIO(Protocol):
    """Side-effect surface — patched in tests so nothing leaves the box."""

    def emit(self, message: str) -> None: ...

    def download_file(self, url: str, dest: Path) -> None: ...

    def extract_archive(self, archive: Path, dest_dir: Path) -> list[Path]: ...

    def run_subprocess(
        self, argv: list[str], *, cwd: Path | None = None, timeout: int = 900
    ) -> tuple[int, str]: ...

    def smoke_test(self, *, binary: Path, gguf: Path, ctx: int) -> tuple[bool, str]: ...


# ─── Default IO implementation (real side effects) ────────────────────


class DefaultWizardIO:
    """Production IO: prints to stdout, hits the network, runs subprocesses."""

    def emit(self, message: str) -> None:
        print(message)

    def download_file(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Stream to disk so big release zips don't sit in RAM.
        with urllib.request.urlopen(url) as resp, dest.open("wb") as fh:
            shutil.copyfileobj(resp, fh, length=1024 * 256)

    def extract_archive(self, archive: Path, dest_dir: Path) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = archive.name.lower()
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(dest_dir)
                return [dest_dir / n for n in zf.namelist()]
        # tar archives — `.tar`, `.tar.gz`, `.tgz`, `.tar.xz`, `.tar.bz2`
        if any(
            name.endswith(suffix)
            for suffix in (".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2")
        ):
            with tarfile.open(archive) as tf:
                tf.extractall(dest_dir)
                return [dest_dir / m.name for m in tf.getmembers()]
        raise ValueError(f"unsupported archive type: {archive.name}")

    def run_subprocess(
        self, argv: list[str], *, cwd: Path | None = None, timeout: int = 900
    ) -> tuple[int, str]:
        try:
            r = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(cwd) if cwd else None,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return 127, str(exc)
        return r.returncode, (r.stdout + r.stderr)[-4000:]

    def smoke_test(self, *, binary: Path, gguf: Path, ctx: int) -> tuple[bool, str]:
        """Spawn the managed server with `-ngl 0` (CPU only — safe even
        when VRAM is busy), wait for `/health`, then unload. Returns
        `(ok, message)` where `message` is human-readable detail."""
        try:
            from oxenclaw.pi.llamacpp_server.manager import (
                LlamaCppServer,
                LlamaCppServerError,
                LlamaCppServerSpec,
            )
        except Exception as exc:  # pragma: no cover — import failures are bugs
            return False, f"manager import failed: {exc}"

        server = LlamaCppServer(binary=binary)
        spec = LlamaCppServerSpec(
            gguf_path=gguf,
            n_ctx=ctx,
            n_gpu_layers=0,  # CPU only — works regardless of VRAM state
            n_threads=4,
            n_parallel=1,
        )
        t0 = time.monotonic()
        try:
            base_url = server.ensure_loaded(spec, health_timeout_s=180.0)
        except LlamaCppServerError as exc:
            return False, str(exc)
        elapsed = time.monotonic() - t0
        try:
            server.unload()
        except Exception:  # pragma: no cover — best-effort cleanup
            pass
        return True, f"healthy at {base_url} in {elapsed:.1f}s"


# ─── Result type ──────────────────────────────────────────────────────


@dataclass
class SetupResult:
    binary_path: Path | None = None
    gguf_path: Path | None = None
    embed_gguf_path: Path | None = None
    env_file: Path | None = None
    rc_appended: Path | None = None
    smoke_ok: bool | None = None
    smoke_detail: str = ""
    notes: list[str] = field(default_factory=list)

    def is_ready(self) -> bool:
        return self.binary_path is not None and self.gguf_path is not None


# ─── Wizard ───────────────────────────────────────────────────────────


_CANCEL_CHOICE = "leave it for now (I'll set it up myself)"
_DEFAULT_LLAMA_REPO = "https://github.com/ggml-org/llama.cpp"


def _candidate_install_dir() -> Path:
    return Path.home() / ".oxenclaw" / "llama.cpp"


def _detect_build_backend() -> tuple[str, list[str]]:
    """Pick a sensible CMake backend flag for this machine.

    Returns `(label, extra_cmake_flags)`. The detection is best-effort
    and biased toward "won't fail to compile" rather than "absolutely
    fastest possible" — operators who care about specific flags
    (`-DGGML_CUDA_F16=ON`, custom arch lists) override via the prompt.
    """
    sysname = platform.system()
    if sysname == "Darwin":
        return "metal", ["-DGGML_METAL=ON"]
    # Linux / WSL2 — prefer CUDA when nvidia-smi is reachable.
    if shutil.which("nvidia-smi"):
        return "cuda", ["-DGGML_CUDA=ON"]
    # Vulkan as a portable GPU fallback if the SDK is installed.
    if shutil.which("glslc") or os.path.isdir("/usr/include/vulkan"):
        return "vulkan", ["-DGGML_VULKAN=ON"]
    return "cpu", []


def _env_file_path() -> Path:
    return Path.home() / ".oxenclaw" / "env"


def _persist_env_lines(env_file: Path, kv: dict[str, str]) -> None:
    """Write or update key=value lines in `env_file` non-destructively.

    Existing lines for the same keys are replaced; unrelated content is
    preserved. Mode is 0644 — these are paths, not secrets.
    """
    env_file.parent.mkdir(parents=True, exist_ok=True)
    existing_lines: list[str] = []
    if env_file.exists():
        existing_lines = env_file.read_text(encoding="utf-8").splitlines()
    keep: list[str] = []
    for ln in existing_lines:
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            keep.append(ln)
            continue
        # Preserve `export FOO=bar` and `FOO=bar` shapes.
        body = stripped
        if body.startswith("export "):
            body = body[len("export ") :]
        head = body.split("=", 1)[0].strip()
        if head in kv:
            continue  # we'll re-emit below
        keep.append(ln)
    keep.append("# oxenclaw setup llamacpp")
    for k, v in kv.items():
        keep.append(f'export {k}="{v}"')
    env_file.write_text("\n".join(keep) + "\n", encoding="utf-8")


def _rc_already_sources(rc_file: Path, env_file: Path) -> bool:
    if not rc_file.exists():
        return False
    needle = f"source {env_file}"
    contents = rc_file.read_text(encoding="utf-8", errors="replace")
    return needle in contents or f". {env_file}" in contents


def _append_rc_source(rc_file: Path, env_file: Path) -> None:
    block = (
        "\n# oxenclaw — load llamacpp-direct setup env\n"
        f"[ -f {env_file} ] && source {env_file}\n"
    )
    with rc_file.open("a", encoding="utf-8") as fh:
        fh.write(block)


def _smoke_binary(binary: Path, io: WizardIO) -> tuple[bool, str]:
    """Run `<binary> --version` to confirm dynamic libs resolve.

    Mirrors the manager's `LD_LIBRARY_PATH` rewrite so prebuilt tarballs
    that ship `.so`s next to the executable don't fail with
    "libllama-common.so.0: cannot open shared object file" — that's the
    ld-loader's fault, not the binary's. Anything else (libsvml.so,
    libcuda.so.1, libtbb.so.12, …) is genuinely missing system runtime
    and the prebuilt is wrong for this host.
    """
    binary_dir = str(binary.resolve().parent)
    existing_ld = os.environ.get("LD_LIBRARY_PATH", "")
    new_ld = f"{binary_dir}:{existing_ld}" if existing_ld else binary_dir
    rc, output = io.run_subprocess(
        [str(binary), "--version"],
        timeout=15,
    )
    # Some IO stubs ignore env munging; in tests we trust the stub's verdict.
    # In production DefaultWizardIO doesn't take env yet — call subprocess
    # directly here so we can pass LD_LIBRARY_PATH precisely.
    try:
        r = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "LD_LIBRARY_PATH": new_ld},
        )
        rc = r.returncode
        output = (r.stdout + r.stderr)[-2000:]
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"smoke spawn failed: {exc}"
    if rc != 0:
        return False, f"--version exited with code {rc}: {output.strip()[-400:]}"
    return True, output.strip().splitlines()[0] if output.strip() else "ok"


def _detect_shell_rc() -> Path | None:
    shell = os.environ.get("SHELL", "").strip()
    home = Path.home()
    if shell.endswith("zsh"):
        return home / ".zshrc"
    if shell.endswith("bash"):
        rc = home / ".bashrc"
        return rc if rc.exists() else (home / ".bash_profile")
    if shell.endswith("fish"):
        return home / ".config" / "fish" / "config.fish"
    # Fall back to bashrc on Linux if SHELL is unset.
    rc = home / ".bashrc"
    return rc if rc.exists() else None


# ─── Step implementations ────────────────────────────────────────────


class LlamaCppSetupWizard:
    def __init__(
        self,
        prompter: Prompter,
        io: WizardIO,
        *,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        self.prompter = prompter
        self.io = io
        # Allow tests to inject a synthetic environment without touching
        # `os.environ`. Falls through to real env if not provided.
        self._env = env_overrides if env_overrides is not None else os.environ

    # — Step 1: binary ────────────────────────────────────────────────

    def step_binary(self) -> Path | None:
        """Resolve a `llama-server` binary, downloading if the user opts in.

        Returns the resolved Path, or None if the user chose to set it
        up later.
        """
        from oxenclaw.pi.llamacpp_server.manager import (
            LlamaCppServerError,
            find_llama_server_binary,
        )

        # Try existing discovery first — including any env override the
        # user already set.
        try:
            existing = find_llama_server_binary()
            self.io.emit(f"  [OK]    llama-server binary found at {existing}")
            return existing
        except LlamaCppServerError:
            pass

        self.io.emit("  [WARN]  llama-server binary not found.")
        backend_label, _ = _detect_build_backend()
        build_choice = (
            f"build from source (git clone + cmake, detected backend: {backend_label}) — recommended"
        )
        prebuilt_choice = "download a prebuilt release zip (paste URL)"
        choice = self.prompter.select(
            "How would you like to install it?",
            choices=[
                build_choice,
                prebuilt_choice,
                "I already have it — let me type the path",
                _CANCEL_CHOICE,
            ],
            default=build_choice,
        )
        if choice == _CANCEL_CHOICE:
            self.io.emit(
                "  Skipping binary install. Re-run `oxenclaw setup llamacpp` "
                "after you've placed `llama-server` on $PATH or set "
                "$OXENCLAW_LLAMACPP_BIN."
            )
            return None

        if choice.startswith("I already have it"):
            raw = self.prompter.text(
                "Absolute path to your llama-server binary",
                default="",
            )
            p = Path(os.path.expanduser(raw)).resolve()
            if not p.is_file() or not os.access(p, os.X_OK):
                self.io.emit(f"  [ERR]   {p} is not an executable file")
                return None
            return p

        if choice.startswith("download a prebuilt"):
            return self._download_binary()

        # Default / build-from-source branch.
        return self._build_from_source()

    def _build_from_source(self) -> Path | None:
        """Clone llama.cpp and build `llama-server` with a backend-aware
        CMake invocation. The clone goes under `~/.oxenclaw/llama.cpp`
        so subsequent runs find it on the discovery path automatically.
        """
        if shutil.which("git") is None:
            self.io.emit("  [ERR]   git not found on PATH; install git and re-run")
            return None
        if shutil.which("cmake") is None:
            self.io.emit("  [ERR]   cmake not found on PATH; install cmake and re-run")
            return None

        repo = self.prompter.text(
            "llama.cpp git repo URL",
            default=_DEFAULT_LLAMA_REPO,
        ).strip()
        if not repo:
            self.io.emit("  [ERR]   repo URL required")
            return None

        backend_label, default_flags = _detect_build_backend()
        self.io.emit(f"  Detected build backend: {backend_label}")
        flag_hint = " ".join(default_flags) if default_flags else "(CPU only)"
        extra_flags_raw = self.prompter.text(
            "Extra CMake flags (override / append; blank to use the detected default)",
            default=flag_hint,
        ).strip()
        if extra_flags_raw and extra_flags_raw != "(CPU only)":
            cmake_flags = extra_flags_raw.split()
        else:
            cmake_flags = list(default_flags)

        clone_dir = _candidate_install_dir()
        if (clone_dir / ".git").is_dir():
            self.io.emit(f"  Reusing existing clone at {clone_dir} (git pull)")
            rc, output = self.io.run_subprocess(
                ["git", "-C", str(clone_dir), "pull", "--ff-only"],
                timeout=300,
            )
            if rc != 0:
                self.io.emit(f"  [WARN]  git pull failed (rc={rc}); continuing with current tree")
                self.io.emit(output[-800:])
        else:
            clone_dir.parent.mkdir(parents=True, exist_ok=True)
            if clone_dir.exists():
                self.io.emit(
                    f"  [ERR]   {clone_dir} exists but is not a git repo; "
                    "move it aside and re-run"
                )
                return None
            self.io.emit(f"  Cloning {repo} → {clone_dir}")
            rc, output = self.io.run_subprocess(
                ["git", "clone", "--depth", "1", repo, str(clone_dir)],
                timeout=600,
            )
            if rc != 0:
                self.io.emit(f"  [ERR]   git clone failed (rc={rc})")
                self.io.emit(output[-2000:])
                return None

        build_dir = clone_dir / "build"
        configure = ["cmake", "-S", str(clone_dir), "-B", str(build_dir)] + cmake_flags
        self.io.emit(f"  Configuring: {' '.join(configure)}")
        rc, output = self.io.run_subprocess(configure, timeout=600)
        if rc != 0:
            self.io.emit(f"  [ERR]   cmake configure failed (rc={rc})")
            self.io.emit(output[-3000:])
            self.io.emit(
                "  Common causes: missing CUDA toolkit / Vulkan SDK / Metal SDK. "
                "Install the toolkit for your detected backend, or re-run with "
                "different CMake flags."
            )
            return None

        # `--target llama-server` skips the long-tail of binaries we don't need.
        jobs = max(1, (os.cpu_count() or 4) - 1)
        build_cmd = [
            "cmake",
            "--build",
            str(build_dir),
            "--config",
            "Release",
            "--target",
            "llama-server",
            "-j",
            str(jobs),
        ]
        self.io.emit(
            f"  Building: {' '.join(build_cmd)} (this can take 5–15 min on CPU; "
            "much faster with CUDA / Metal)"
        )
        rc, output = self.io.run_subprocess(build_cmd, timeout=2700)
        if rc != 0:
            self.io.emit(f"  [ERR]   cmake build failed (rc={rc})")
            self.io.emit(output[-3000:])
            return None

        candidates = [
            build_dir / "bin" / "llama-server",
            build_dir / "bin" / "Release" / "llama-server.exe",
            build_dir / "llama-server",
        ]
        for c in candidates:
            if c.is_file():
                try:
                    c.chmod(0o755)
                except OSError:
                    pass
                self.io.emit(f"  [OK]    binary at {c}")
                return c
        # Last resort: walk the build tree.
        for c in build_dir.rglob("llama-server*"):
            if c.is_file() and c.name in {"llama-server", "llama-server.exe"}:
                try:
                    c.chmod(0o755)
                except OSError:
                    pass
                self.io.emit(f"  [OK]    binary at {c}")
                return c
        self.io.emit(
            f"  [ERR]   build reported success but no llama-server under {build_dir}"
        )
        return None

    _SUPPORTED_ARCHIVE_SUFFIXES = (
        ".zip",
        ".tar",
        ".tar.gz",
        ".tgz",
        ".tar.xz",
        ".txz",
        ".tar.bz2",
        ".tbz2",
    )

    def _prompt_release_url(self) -> str | None:
        """Prompt for a llama.cpp release asset URL, re-prompting up to
        3 times on validation errors. Returns None if the user gives up.

        Validation: must be `http(s)://`, must end in a known archive
        suffix (`.zip`, `.tar.gz`, `.tgz`, `.tar.xz`, `.tar.bz2`).
        """
        for attempt in range(3):
            url = (
                self.prompter.text(
                    "Release asset URL (https://...; .zip / .tar.gz / .tar.xz)"
                    if attempt == 0
                    else "Try again — paste the full https:// URL of the asset",
                    default="",
                )
                or ""
            ).strip()
            if not url:
                self.io.emit("  [ERR]   URL is required")
                continue
            if not (url.startswith("http://") or url.startswith("https://")):
                self.io.emit(
                    f"  [ERR]   not a valid http/https URL: {url!r} — "
                    "make sure you copied the full link from the release page"
                )
                continue
            lower = url.lower().split("?", 1)[0]
            if not any(lower.endswith(s) for s in self._SUPPORTED_ARCHIVE_SUFFIXES):
                self.io.emit(
                    f"  [ERR]   unsupported archive type in {url!r}; "
                    f"supported: {', '.join(self._SUPPORTED_ARCHIVE_SUFFIXES)}"
                )
                continue
            return url
        self.io.emit("  Giving up after 3 invalid URLs. Re-run when you have the right link.")
        return None

    def _download_binary(self) -> Path | None:
        self.io.emit(
            "  Browse https://github.com/ggml-org/llama.cpp/releases, copy "
            "the URL of the asset matching your platform"
        )
        self.io.emit(
            "  (Linux+CUDA: llama-<ver>-bin-ubuntu-x64-cuda.{zip,tar.gz} / "
            "macOS: llama-<ver>-bin-macos-arm64.zip / "
            "Win+CUDA: llama-<ver>-bin-win-cuda-x64.zip)"
        )
        url = self._prompt_release_url()
        if url is None:
            return None

        install_dir = _candidate_install_dir()
        archive = install_dir / Path(url.split("?", 1)[0].split("/")[-1])
        try:
            self.io.emit(f"  Downloading {url} → {archive}")
            self.io.download_file(url, archive)
        except Exception as exc:
            self.io.emit(f"  [ERR]   download failed: {exc}")
            return None

        try:
            self.io.emit(f"  Extracting → {install_dir}")
            extracted = self.io.extract_archive(archive, install_dir)
        except Exception as exc:
            self.io.emit(f"  [ERR]   extract failed: {exc}")
            return None

        # Find `llama-server` (exact name on Linux/macOS, `.exe` on Windows)
        # anywhere under the extracted tree.
        candidates: list[Path] = []
        for p in extracted:
            if p.is_dir():
                continue
            if p.name in {"llama-server", "llama-server.exe"}:
                candidates.append(p)
        if not candidates:
            # `extracted` may include only top-level entries; walk the tree.
            for p in install_dir.rglob("llama-server*"):
                if p.is_file() and p.name in {"llama-server", "llama-server.exe"}:
                    candidates.append(p)
        if not candidates:
            self.io.emit(
                f"  [ERR]   archive extracted but no llama-server binary "
                f"under {install_dir}"
            )
            return None
        binary = candidates[0]
        try:
            binary.chmod(0o755)
        except OSError:
            pass
        # Sanity check: prebuilt assets sometimes target a backend the
        # host can't satisfy (e.g. a SYCL build downloaded onto an NVIDIA
        # box that lacks Intel oneAPI's `libsvml.so`). The binary then
        # exits with code 127 the first time it loads its plugins —
        # which is exactly the trap that bit us. Run `--version` with
        # the same `LD_LIBRARY_PATH` rewrite the manager will use, so
        # we catch the mismatch here instead of mid-chat.
        ok, detail = _smoke_binary(binary, self.io)
        if not ok:
            self.io.emit(
                "  [ERR]   binary won't run on this host — likely a "
                "wrong-architecture prebuilt (SYCL/ROCm/Vulkan asset on "
                "a CPU/CUDA-only box). Re-run the wizard and pick "
                "Option 1 (build from source) so cmake configures for "
                "your real backend."
            )
            self.io.emit(f"          {detail}")
            return None
        self.io.emit(f"  [OK]    binary at {binary}")
        return binary

    # — Step 2: GGUF ──────────────────────────────────────────────────

    def step_gguf(self) -> Path | None:
        """Resolve a GGUF path. Optionally drives `hf download`."""
        existing_env = self._env.get("OXENCLAW_LLAMACPP_GGUF", "").strip()
        if existing_env:
            p = Path(os.path.expanduser(existing_env))
            if p.is_file():
                self.io.emit(f"  [OK]    $OXENCLAW_LLAMACPP_GGUF → {p}")
                return p
            self.io.emit(
                f"  [WARN]  $OXENCLAW_LLAMACPP_GGUF set to {p} but the "
                "file is missing; will re-prompt"
            )

        choice = self.prompter.select(
            "Where's the GGUF you want to serve?",
            choices=[
                "I have one on disk — let me type the path",
                "download from Hugging Face via `hf download`",
                _CANCEL_CHOICE,
            ],
            default="I have one on disk — let me type the path",
        )
        if choice == _CANCEL_CHOICE:
            self.io.emit(
                "  Skipping GGUF. Re-run after placing one and pointing "
                "$OXENCLAW_LLAMACPP_GGUF at it."
            )
            return None
        if choice.startswith("I have one on disk"):
            raw = self.prompter.text("Absolute path to GGUF", default="")
            p = Path(os.path.expanduser(raw)).resolve()
            if not p.is_file():
                self.io.emit(f"  [ERR]   {p} is not a file")
                return None
            return p
        return self._download_gguf()

    def _download_gguf(self) -> Path | None:
        # Defaults are the model we live-tested in
        # docs/LLAMACPP_DIRECT.md: gemma-4-E4B-it-UD-Q4_K_XL — multimodal,
        # tool-calling, ~4.8 GiB, fits an 8 GiB GPU comfortably.
        repo = self.prompter.text(
            "Hugging Face repo",
            default="unsloth/gemma-4-E4B-it-GGUF",
        ).strip()
        filename = self.prompter.text(
            "Exact GGUF filename in that repo",
            default="gemma-4-E4B-it-UD-Q4_K_XL.gguf",
        ).strip()
        if not repo or not filename:
            self.io.emit("  [ERR]   repo and filename are both required")
            return None
        local_dir_raw = self.prompter.text(
            "Where to store the GGUF",
            default=str(Path.home() / "models"),
        ).strip()
        local_dir = Path(os.path.expanduser(local_dir_raw)).resolve()
        local_dir.mkdir(parents=True, exist_ok=True)

        # Use `hf download` (the modern CLI) — `huggingface-cli download`
        # was renamed in huggingface_hub 0.24+.
        argv = [
            "hf",
            "download",
            repo,
            filename,
            "--local-dir",
            str(local_dir),
        ]
        self.io.emit(f"  Running: {' '.join(argv)}")
        rc, output = self.io.run_subprocess(argv)
        if rc != 0:
            # Fallback to the deprecated CLI name in case the user has
            # an older huggingface_hub.
            self.io.emit(f"  [WARN]  `hf download` failed (rc={rc}); trying `huggingface-cli`")
            argv2 = ["huggingface-cli", "download", repo, filename, "--local-dir", str(local_dir)]
            rc, output = self.io.run_subprocess(argv2)
        if rc != 0:
            self.io.emit(f"  [ERR]   download failed (rc={rc})\n{output[:1000]}")
            self.io.emit(
                "  Install huggingface_hub first: `pip install -U \"huggingface_hub[cli]\"`"
            )
            return None

        candidate = local_dir / filename
        if not candidate.is_file():
            # Some files are sharded; settle for any new gguf under local_dir.
            ggufs = list(local_dir.glob(f"{filename}*"))
            if not ggufs:
                self.io.emit(f"  [ERR]   download finished but no file matching {filename}")
                return None
            candidate = ggufs[0]
        size_mb = candidate.stat().st_size / (1024 * 1024)
        self.io.emit(f"  [OK]    GGUF at {candidate} ({size_mb:.0f} MiB)")
        return candidate

    # — Step 2.5: optional embedding GGUF ─────────────────────────────

    def step_embed_gguf(self) -> Path | None:
        """Optional — set up an embedding GGUF so memory features stop
        depending on Ollama. Returns None if the user declines.

        The default suggestion (`nomic-ai/nomic-embed-text-v2-moe-GGUF`,
        the project's own GGUF release) is multilingual + small
        (~328 MiB Q4_K_M) so it cohabits well with a chat model on a
        single 8 GiB GPU.
        """
        existing_env = self._env.get("OXENCLAW_LLAMACPP_EMBED_GGUF", "").strip()
        if existing_env:
            p = Path(os.path.expanduser(existing_env))
            if p.is_file():
                self.io.emit(f"  [OK]    $OXENCLAW_LLAMACPP_EMBED_GGUF → {p}")
                return p
            self.io.emit(
                f"  [WARN]  $OXENCLAW_LLAMACPP_EMBED_GGUF set to {p} but the "
                "file is missing; will re-prompt"
            )

        if not self.prompter.confirm(
            "Set up llama.cpp-direct embeddings now? (replaces Ollama for the "
            "memory pipeline; runs as a separate llama-server instance)",
            default=True,
        ):
            self.io.emit("  Skipping embedding setup; embeddings will keep using ollama.")
            return None

        choice = self.prompter.select(
            "Where's the embedding GGUF?",
            choices=[
                "I have one on disk — let me type the path",
                "download from Hugging Face via `hf download`",
                _CANCEL_CHOICE,
            ],
            default="download from Hugging Face via `hf download`",
        )
        if choice == _CANCEL_CHOICE:
            self.io.emit("  Skipping embedding setup.")
            return None
        if choice.startswith("I have one on disk"):
            raw = self.prompter.text("Absolute path to embedding GGUF", default="")
            p = Path(os.path.expanduser(raw)).resolve()
            if not p.is_file():
                self.io.emit(f"  [ERR]   {p} is not a file")
                return None
            return p

        repo = self.prompter.text(
            "Hugging Face repo",
            default="nomic-ai/nomic-embed-text-v2-moe-GGUF",
        ).strip()
        filename = self.prompter.text(
            "Exact GGUF filename in that repo",
            default="nomic-embed-text-v2-moe.Q4_K_M.gguf",
        ).strip()
        if not repo or not filename:
            self.io.emit("  [ERR]   repo and filename are both required")
            return None
        local_dir_raw = self.prompter.text(
            "Where to store the embedding GGUF",
            default=str(Path.home() / "models"),
        ).strip()
        local_dir = Path(os.path.expanduser(local_dir_raw)).resolve()
        local_dir.mkdir(parents=True, exist_ok=True)

        argv = ["hf", "download", repo, filename, "--local-dir", str(local_dir)]
        self.io.emit(f"  Running: {' '.join(argv)}")
        rc, output = self.io.run_subprocess(argv)
        if rc != 0:
            argv2 = ["huggingface-cli", "download", repo, filename, "--local-dir", str(local_dir)]
            rc, output = self.io.run_subprocess(argv2)
        if rc != 0:
            self.io.emit(f"  [ERR]   download failed (rc={rc})\n{output[:1000]}")
            return None

        candidate = local_dir / filename
        if not candidate.is_file():
            ggufs = list(local_dir.glob(f"{filename}*"))
            if not ggufs:
                self.io.emit(f"  [ERR]   download finished but no file matching {filename}")
                return None
            candidate = ggufs[0]
        size_mb = candidate.stat().st_size / (1024 * 1024)
        self.io.emit(f"  [OK]    embedding GGUF at {candidate} ({size_mb:.0f} MiB)")
        return candidate

    # — Step 3: persist env ───────────────────────────────────────────

    def step_persist(
        self,
        *,
        binary: Path,
        gguf: Path,
        embed_gguf: Path | None = None,
    ) -> tuple[Path, Path | None]:
        """Write paths to `~/.oxenclaw/env` and (optionally) source it from rc."""
        env_file = _env_file_path()
        kv: dict[str, str] = {
            "OXENCLAW_LLAMACPP_BIN": str(binary),
            "OXENCLAW_LLAMACPP_GGUF": str(gguf),
        }
        if embed_gguf is not None:
            kv["OXENCLAW_LLAMACPP_EMBED_GGUF"] = str(embed_gguf)
            # Switch the embedder factory default to the managed path so
            # the gateway picks it up automatically on next start.
            kv["OXENCLAW_EMBEDDER"] = "llamacpp-direct"
        _persist_env_lines(env_file, kv)
        self.io.emit(f"  [OK]    persisted env → {env_file}")

        rc = _detect_shell_rc()
        rc_touched: Path | None = None
        if rc is not None and not _rc_already_sources(rc, env_file):
            if self.prompter.confirm(
                f"Append `source {env_file}` to {rc} so future shells pick "
                "this up automatically?",
                default=True,
            ):
                _append_rc_source(rc, env_file)
                rc_touched = rc
                self.io.emit(f"  [OK]    appended source line → {rc}")
            else:
                self.io.emit(
                    f"  Skipped rc edit. Run `source {env_file}` in any "
                    "shell that needs the vars."
                )
        elif rc is not None:
            self.io.emit(f"  [OK]    {rc} already sources {env_file}")
        return env_file, rc_touched

    # — Step 4: smoke test ───────────────────────────────────────────

    def step_smoke_test(self, *, binary: Path, gguf: Path) -> tuple[bool, str]:
        """Spin up the managed server briefly to confirm everything fits."""
        if not self.prompter.confirm(
            "Run a quick CPU-only smoke test (spawns llama-server briefly)?",
            default=True,
        ):
            return True, "skipped by user"
        ok, detail = self.io.smoke_test(binary=binary, gguf=gguf, ctx=2048)
        glyph = "[OK]" if ok else "[ERR]"
        self.io.emit(f"  {glyph}    smoke test: {detail}")
        return ok, detail

    # — Driver ────────────────────────────────────────────────────────

    def run(self) -> SetupResult:
        result = SetupResult()
        self.io.emit("oxenclaw setup llamacpp — one-shot llamacpp-direct setup")
        self.io.emit("")

        self.io.emit("Step 1/5 — llama-server binary")
        result.binary_path = self.step_binary()
        if result.binary_path is None:
            return result

        self.io.emit("")
        self.io.emit("Step 2/5 — chat GGUF weights")
        result.gguf_path = self.step_gguf()
        if result.gguf_path is None:
            return result

        self.io.emit("")
        self.io.emit("Step 3/5 — embedding GGUF (optional, replaces Ollama)")
        result.embed_gguf_path = self.step_embed_gguf()

        self.io.emit("")
        self.io.emit("Step 4/5 — persist env vars")
        result.env_file, result.rc_appended = self.step_persist(
            binary=result.binary_path,
            gguf=result.gguf_path,
            embed_gguf=result.embed_gguf_path,
        )

        self.io.emit("")
        self.io.emit("Step 5/5 — smoke test")
        ok, detail = self.step_smoke_test(binary=result.binary_path, gguf=result.gguf_path)
        result.smoke_ok = ok
        result.smoke_detail = detail

        self.io.emit("")
        self.io.emit("Done. Next steps:")
        self.io.emit(
            "  - Restart any running `oxenclaw gateway` so it picks up the "
            "new env (the CLI auto-loads ~/.oxenclaw/env on every entry)."
        )
        self.io.emit("  - `oxenclaw doctor` should now show llamacpp-direct as OK.")
        self.io.emit("  - `oxenclaw gateway start` will pick `llamacpp-direct` automatically.")
        return result


__all__ = [
    "DefaultWizardIO",
    "LlamaCppSetupWizard",
    "Prompter",
    "SetupResult",
    "WizardIO",
]
