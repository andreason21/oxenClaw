"""Tests for the `oxenclaw setup llamacpp` wizard + the doctor probe.

The wizard's IO surface is mocked end-to-end (no subprocess, no
network, no actual `llama-server` spawn), so these tests run in
milliseconds and never touch the user's shell rc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

from oxenclaw.flows.doctor import DoctorReport, _probe_llamacpp_direct
from oxenclaw.flows.llamacpp_setup import (
    LlamaCppSetupWizard,
)

# ─── Doctor probe ────────────────────────────────────────────────────


def test_doctor_probe_warns_when_nothing_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OXENCLAW_LLAMACPP_GGUF", raising=False)
    monkeypatch.delenv("OXENCLAW_LLAMACPP_BIN", raising=False)
    monkeypatch.delenv("LLAMA_SERVER_PATH", raising=False)
    monkeypatch.delenv("UNSLOTH_LLAMA_CPP_PATH", raising=False)

    with (
        patch("shutil.which", return_value=None),
        patch(
            "oxenclaw.pi.llamacpp_server.manager._candidate_install_dirs",
            return_value=[],
        ),
    ):
        report = DoctorReport()
        _probe_llamacpp_direct(report)

    findings = [f for f in report.findings if f.area == "llamacpp-direct"]
    assert len(findings) == 1
    assert findings[0].severity == "warn"
    assert "oxenclaw setup llamacpp" in findings[0].message


def test_doctor_probe_ok_when_both_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"\x00" * 1024)

    monkeypatch.setenv("OXENCLAW_LLAMACPP_BIN", str(binary))
    monkeypatch.setenv("OXENCLAW_LLAMACPP_GGUF", str(gguf))

    report = DoctorReport()
    _probe_llamacpp_direct(report)

    findings = [f for f in report.findings if f.area == "llamacpp-direct"]
    assert len(findings) == 1
    assert findings[0].severity == "ok"
    assert findings[0].message == "ready"


def test_doctor_probe_warns_when_gguf_missing_but_binary_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("OXENCLAW_LLAMACPP_BIN", str(binary))
    monkeypatch.delenv("OXENCLAW_LLAMACPP_GGUF", raising=False)

    report = DoctorReport()
    _probe_llamacpp_direct(report)

    findings = [f for f in report.findings if f.area == "llamacpp-direct"]
    assert len(findings) == 1
    assert findings[0].severity == "warn"
    assert "OXENCLAW_LLAMACPP_GGUF" in (findings[0].detail or "")


# ─── Wizard scaffolding ──────────────────────────────────────────────


@dataclass
class _StubPrompter:
    """Replays a fixed answer script. `select` / `text` / `confirm`
    each pop from a separate FIFO."""

    selects: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    confirms: list[bool] = field(default_factory=list)

    def select(self, message: str, choices: list[str], *, default: str | None = None) -> str:
        return self.selects.pop(0)

    def text(self, message: str, *, default: str | None = None, secret: bool = False) -> str:
        return self.texts.pop(0)

    def confirm(self, message: str, *, default: bool = True) -> bool:
        return self.confirms.pop(0)


@dataclass
class _StubIO:
    """Records side effects without performing any."""

    messages: list[str] = field(default_factory=list)
    download_calls: list[tuple[str, Path]] = field(default_factory=list)
    extract_calls: list[tuple[Path, Path]] = field(default_factory=list)
    subprocess_calls: list[list[str]] = field(default_factory=list)
    smoke_calls: list[tuple[Path, Path, int]] = field(default_factory=list)
    smoke_result: tuple[bool, str] = (True, "stub-ok")

    def emit(self, message: str) -> None:
        self.messages.append(message)

    def download_file(self, url: str, dest: Path) -> None:
        self.download_calls.append((url, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Fake a zip file the extractor can consume.
        dest.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    def extract_archive(self, archive: Path, dest_dir: Path) -> list[Path]:
        self.extract_calls.append((archive, dest_dir))
        # Pretend the archive contained `build/bin/llama-server`.
        dest_dir.mkdir(parents=True, exist_ok=True)
        binary = dest_dir / "build" / "bin" / "llama-server"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        return [binary]

    def run_subprocess(
        self, argv: list[str], *, cwd: Path | None = None, timeout: int = 900
    ) -> tuple[int, str]:
        self.subprocess_calls.append(argv)
        return 0, "fake-ok"

    def smoke_test(self, *, binary: Path, gguf: Path, ctx: int) -> tuple[bool, str]:
        self.smoke_calls.append((binary, gguf, ctx))
        return self.smoke_result


# ─── Wizard happy paths ──────────────────────────────────────────────


def test_wizard_happy_path_existing_binary_and_gguf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both prerequisites already present: wizard should short-circuit
    binary + GGUF detection, then persist + smoke-test."""
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"\x00" * 1024)
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    monkeypatch.setenv("OXENCLAW_LLAMACPP_BIN", str(binary))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    prompter = _StubPrompter(
        # No selects expected — both prerequisites already discovered.
        confirms=[
            False,  # decline embedding setup (Step 3)
            False,  # don't append source line to rc
            True,  # run smoke test
        ],
    )
    io = _StubIO()
    wizard = LlamaCppSetupWizard(
        prompter=prompter,
        io=io,
        env_overrides={"OXENCLAW_LLAMACPP_GGUF": str(gguf)},
    )

    result = wizard.run()
    assert result.is_ready()
    assert result.binary_path == binary
    assert result.gguf_path == gguf
    assert result.env_file == fake_home / ".oxenclaw" / "env"
    assert result.smoke_ok is True
    # Env file has both keys.
    body = result.env_file.read_text(encoding="utf-8")
    assert f'OXENCLAW_LLAMACPP_BIN="{binary}"' in body
    assert f'OXENCLAW_LLAMACPP_GGUF="{gguf}"' in body
    # Smoke test was driven through our IO, not real spawn.
    assert io.smoke_calls == [(binary, gguf, 2048)]


def test_wizard_downloads_binary_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Prebuilt-zip fallback path (the user picks Option B)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"\x00" * 1024)

    monkeypatch.delenv("OXENCLAW_LLAMACPP_BIN", raising=False)
    monkeypatch.delenv("LLAMA_SERVER_PATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # Force discovery to fail so the wizard takes the install branch.
    with (
        patch("shutil.which", return_value=None),
        patch(
            "oxenclaw.pi.llamacpp_server.manager._candidate_install_dirs",
            return_value=[fake_home / ".oxenclaw" / "llama.cpp"],
        ),
    ):
        prompter = _StubPrompter(
            selects=["download a prebuilt release zip (paste URL)"],
            texts=["https://example.test/llama-bin.zip"],
            confirms=[
                False,  # decline embedding setup
                False,  # don't append rc
                False,  # skip smoke test
            ],
        )
        io = _StubIO()
        wizard = LlamaCppSetupWizard(
            prompter=prompter,
            io=io,
            env_overrides={"OXENCLAW_LLAMACPP_GGUF": str(gguf)},
        )
        result = wizard.run()

    assert io.download_calls and io.download_calls[0][0] == "https://example.test/llama-bin.zip"
    assert io.extract_calls
    # Binary discovered under the extracted tree.
    assert result.binary_path is not None
    assert result.binary_path.name == "llama-server"
    assert result.is_ready()


def test_wizard_accepts_tar_gz_release_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The user's bug: pasting a `.tar.gz` release URL should not fail
    URL validation. Wizard now accepts every common archive suffix."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"\x00" * 1024)
    monkeypatch.delenv("OXENCLAW_LLAMACPP_BIN", raising=False)
    monkeypatch.delenv("LLAMA_SERVER_PATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    tar_url = (
        "https://github.com/ggml-org/llama.cpp/releases/download/b8967/"
        "llama-b8967-bin-ubuntu-sycl-fp16-x64.tar.gz"
    )

    with (
        patch("shutil.which", return_value=None),
        patch(
            "oxenclaw.pi.llamacpp_server.manager._candidate_install_dirs",
            return_value=[fake_home / ".oxenclaw" / "llama.cpp"],
        ),
    ):
        prompter = _StubPrompter(
            selects=["download a prebuilt release zip (paste URL)"],
            texts=[tar_url],
            confirms=[False, False, False],  # decline embed, no rc edit, skip smoke
        )
        io = _StubIO()
        wizard = LlamaCppSetupWizard(
            prompter=prompter,
            io=io,
            env_overrides={"OXENCLAW_LLAMACPP_GGUF": str(gguf)},
        )
        result = wizard.run()

    # URL validation accepted .tar.gz and download proceeded.
    assert io.download_calls and io.download_calls[0][0] == tar_url
    # Archive name on disk preserves the original suffix.
    archive_path = io.download_calls[0][1]
    assert archive_path.name.endswith(".tar.gz")
    assert io.extract_calls
    assert result.is_ready()


def test_wizard_reprompts_on_bad_url_then_gives_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Three invalid URLs in a row → wizard returns None instead of
    crashing. Original bug: a single invalid URL terminated the flow."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.delenv("OXENCLAW_LLAMACPP_BIN", raising=False)
    monkeypatch.delenv("LLAMA_SERVER_PATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    with (
        patch("shutil.which", return_value=None),
        patch(
            "oxenclaw.pi.llamacpp_server.manager._candidate_install_dirs",
            return_value=[fake_home / ".oxenclaw" / "llama.cpp"],
        ),
    ):
        prompter = _StubPrompter(
            selects=["download a prebuilt release zip (paste URL)"],
            texts=[
                "llama-b8967-bin-ubuntu-sycl-fp16-x64.tar.gz",  # missing scheme
                "ftp://example.test/x.tar.gz",  # wrong scheme
                "https://example.test/x.exe",  # wrong suffix
            ],
        )
        io = _StubIO()
        wizard = LlamaCppSetupWizard(
            prompter=prompter,
            io=io,
            env_overrides={},
        )
        binary = wizard.step_binary()

    assert binary is None
    # Each attempt produced an [ERR] line.
    err_lines = [m for m in io.messages if "[ERR]" in m]
    assert len(err_lines) >= 3


def test_wizard_builds_from_source_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default path: git clone + cmake configure + cmake build."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"\x00" * 1024)

    monkeypatch.delenv("OXENCLAW_LLAMACPP_BIN", raising=False)
    monkeypatch.delenv("LLAMA_SERVER_PATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    install_dir = fake_home / ".oxenclaw" / "llama.cpp"
    build_bin_dir = install_dir / "build" / "bin"

    class _BuildIO(_StubIO):
        def run_subprocess(
            self, argv: list[str], *, cwd: Path | None = None, timeout: int = 900
        ) -> tuple[int, str]:
            self.subprocess_calls.append(argv)
            # Fake the cmake build: drop a binary at build/bin/llama-server.
            if argv and argv[0] == "cmake" and "--build" in argv:
                build_bin_dir.mkdir(parents=True, exist_ok=True)
                binary = build_bin_dir / "llama-server"
                binary.write_text("#!/bin/sh\nexit 0\n")
                binary.chmod(0o755)
            elif argv and argv[0] == "git" and "clone" in argv:
                # Fake git clone — create the repo dir + .git marker.
                install_dir.mkdir(parents=True, exist_ok=True)
                (install_dir / ".git").mkdir(exist_ok=True)
            return 0, "ok"

    # Force discovery to fail; pretend git+cmake are on PATH.
    def _fake_which(name: str) -> str | None:
        if name in {"git", "cmake"}:
            return f"/usr/bin/{name}"
        return None

    with (
        patch("shutil.which", side_effect=_fake_which),
        patch(
            "oxenclaw.pi.llamacpp_server.manager._candidate_install_dirs",
            return_value=[install_dir],
        ),
    ):
        prompter = _StubPrompter(
            # Builder choice label is dynamic ("(detected backend: cpu)" etc.)
            # — match by prefix via select stub returning what the wizard
            # offered. Easiest: provide the exact prefix string the wizard
            # produces, since `_StubPrompter.select` ignores `choices` and
            # returns whatever we supplied.
            selects=["build from source"],
            texts=[
                "https://github.com/ggml-org/llama.cpp",  # repo url default accepted
                "",  # accept detected cmake flags
            ],
            confirms=[False, False, False],  # decline embed, no rc edit, skip smoke
        )
        io = _BuildIO()
        wizard = LlamaCppSetupWizard(
            prompter=prompter,
            io=io,
            env_overrides={"OXENCLAW_LLAMACPP_GGUF": str(gguf)},
        )
        result = wizard.run()

    # Expect: at least git-clone + cmake-configure + cmake-build subprocess calls.
    cmd_heads = [argv[0] for argv in io.subprocess_calls]
    assert "git" in cmd_heads
    assert cmd_heads.count("cmake") >= 2  # configure + build
    assert result.binary_path == build_bin_dir / "llama-server"
    assert result.is_ready()


def test_wizard_downloads_gguf_via_hf_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("OXENCLAW_LLAMACPP_BIN", str(binary))
    monkeypatch.delenv("OXENCLAW_LLAMACPP_GGUF", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    target_dir = fake_home / "models"

    class _DownloadingIO(_StubIO):
        def run_subprocess(self, argv: list[str]) -> tuple[int, str]:
            self.subprocess_calls.append(argv)
            # Fake the file `hf download` would have produced.
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "x.gguf").write_bytes(b"\x00" * 1024)
            return 0, "downloaded"

    prompter = _StubPrompter(
        selects=["download from Hugging Face via `hf download`"],
        texts=["unsloth/x-GGUF", "x.gguf", str(target_dir)],
        confirms=[False, False, False],  # decline embed, no rc edit, no smoke
    )
    io = _DownloadingIO()
    wizard = LlamaCppSetupWizard(
        prompter=prompter,
        io=io,
        env_overrides={},
    )
    result = wizard.run()

    assert io.subprocess_calls
    assert io.subprocess_calls[0][:2] == ["hf", "download"]
    assert result.gguf_path == target_dir / "x.gguf"
    assert result.is_ready()


# ─── Wizard failure paths ────────────────────────────────────────────


def test_wizard_aborts_when_user_declines_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.delenv("OXENCLAW_LLAMACPP_BIN", raising=False)
    monkeypatch.delenv("LLAMA_SERVER_PATH", raising=False)
    monkeypatch.delenv("UNSLOTH_LLAMA_CPP_PATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    with (
        patch("shutil.which", return_value=None),
        patch(
            "oxenclaw.pi.llamacpp_server.manager._candidate_install_dirs",
            return_value=[],
        ),
    ):
        prompter = _StubPrompter(
            selects=["leave it for now (I'll set it up myself)"],
        )
        io = _StubIO()
        wizard = LlamaCppSetupWizard(prompter=prompter, io=io, env_overrides={})
        result = wizard.run()

    assert result.binary_path is None
    assert not result.is_ready()


def test_wizard_rejects_binary_that_fails_smoke(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SYCL prebuilt on a CUDA box → `llama-server --version` exits 127
    because libsvml.so is missing. Wizard must catch this at install
    time instead of letting it cascade into mid-chat embedding errors.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"\x00" * 1024)
    monkeypatch.delenv("OXENCLAW_LLAMACPP_BIN", raising=False)
    monkeypatch.delenv("LLAMA_SERVER_PATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    install_dir = fake_home / ".oxenclaw" / "llama.cpp"

    class _BadBinaryIO(_StubIO):
        def extract_archive(self, archive: Path, dest_dir: Path) -> list[Path]:
            self.extract_calls.append((archive, dest_dir))
            dest_dir.mkdir(parents=True, exist_ok=True)
            binary = dest_dir / "build" / "bin" / "llama-server"
            binary.parent.mkdir(parents=True, exist_ok=True)
            # Wrong-arch binary: pretend it's a real SYCL prebuilt that
            # exits 127 at startup because libsvml.so is missing.
            binary.write_text("#!/bin/sh\nexit 127\n")
            binary.chmod(0o755)
            return [binary]

    with (
        patch("shutil.which", return_value=None),
        patch(
            "oxenclaw.pi.llamacpp_server.manager._candidate_install_dirs",
            return_value=[install_dir],
        ),
    ):
        prompter = _StubPrompter(
            selects=["download a prebuilt release zip (paste URL)"],
            texts=["https://example.test/llama-sycl.tar.gz"],
        )
        io = _BadBinaryIO()
        wizard = LlamaCppSetupWizard(
            prompter=prompter,
            io=io,
            env_overrides={"OXENCLAW_LLAMACPP_GGUF": str(gguf)},
        )
        result = wizard.run()

    # Wizard must not certify the binary as ready.
    assert result.binary_path is None
    assert not result.is_ready()
    err_lines = [m for m in io.messages if "[ERR]" in m or "won't run" in m]
    assert any("won't run" in m or "wrong-architecture" in m for m in io.messages)


def test_wizard_smoke_failure_propagates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"\x00" * 1024)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("OXENCLAW_LLAMACPP_BIN", str(binary))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    prompter = _StubPrompter(
        confirms=[False, False, True],  # decline embed, no rc edit, run smoke
    )
    io = _StubIO()
    io.smoke_result = (False, "simulated spawn failure")
    wizard = LlamaCppSetupWizard(
        prompter=prompter,
        io=io,
        env_overrides={"OXENCLAW_LLAMACPP_GGUF": str(gguf)},
    )
    result = wizard.run()

    assert result.is_ready()  # binary + gguf resolved
    assert result.smoke_ok is False
    assert "simulated" in result.smoke_detail


# ─── env file persistence ────────────────────────────────────────────


@pytest.fixture
def _env_isolation():
    """Snapshot/restore every OXENCLAW_LLAMACPP_* env var around the test.

    `monkeypatch.delenv` only undoes its *own* writes — direct
    `os.environ[k] = v` calls (which `load_oxenclaw_env_file` makes by
    design) leak into adjacent tests. This fixture is the explicit
    teardown those tests need.
    """
    import os as _os

    keys = ("OXENCLAW_LLAMACPP_BIN", "OXENCLAW_LLAMACPP_GGUF")
    snapshot = {k: _os.environ.get(k) for k in keys}
    for k in keys:
        _os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in snapshot.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


def test_env_autoload_applies_persisted_keys(_env_isolation, tmp_path: Path) -> None:
    """`~/.oxenclaw/env` written by the wizard must flow into os.environ
    on every CLI entry — otherwise `--provider auto` silently falls
    back to ollama even though the user ran the wizard."""
    from oxenclaw.config.env_loader import load_oxenclaw_env_file

    env_file = tmp_path / "env"
    env_file.write_text(
        "# oxenclaw setup llamacpp\n"
        'export OXENCLAW_LLAMACPP_BIN="/test/llama-server"\n'
        'export OXENCLAW_LLAMACPP_GGUF="/test/m.gguf"\n',
        encoding="utf-8",
    )

    n = load_oxenclaw_env_file(path=env_file)
    assert n == 2
    import os as _os

    assert _os.environ["OXENCLAW_LLAMACPP_BIN"] == "/test/llama-server"
    assert _os.environ["OXENCLAW_LLAMACPP_GGUF"] == "/test/m.gguf"


def test_env_autoload_does_not_override_shell_by_default(_env_isolation, tmp_path: Path) -> None:
    """Shell-set vars must win — operator's `export FOO=bar` is the
    escape hatch."""
    import os as _os

    from oxenclaw.config.env_loader import load_oxenclaw_env_file

    env_file = tmp_path / "env"
    env_file.write_text('OXENCLAW_LLAMACPP_GGUF="/from-file"\n', encoding="utf-8")
    _os.environ["OXENCLAW_LLAMACPP_GGUF"] = "/from-shell"

    load_oxenclaw_env_file(path=env_file)
    assert _os.environ["OXENCLAW_LLAMACPP_GGUF"] == "/from-shell"

    # override=True flips the precedence.
    load_oxenclaw_env_file(path=env_file, override=True)
    assert _os.environ["OXENCLAW_LLAMACPP_GGUF"] == "/from-file"


def test_env_autoload_handles_malformed_lines(tmp_path: Path) -> None:
    """A broken file should not blow up CLI startup — it returns 0 and
    leaves os.environ untouched for that key."""
    from oxenclaw.config.env_loader import parse_env_file

    parsed = parse_env_file(
        "# comment\n"
        "\n"
        "BAD LINE NO EQUALS\n"
        "OK_KEY=ok\n"
        "export ANOTHER='spaces are fine'\n"
        "= value-with-no-key\n"
        "1BAD=numeric-prefix\n"
    )
    assert parsed == {"OK_KEY": "ok", "ANOTHER": "spaces are fine"}


def test_env_autoload_no_file_is_noop(tmp_path: Path) -> None:
    from oxenclaw.config.env_loader import load_oxenclaw_env_file

    n = load_oxenclaw_env_file(path=tmp_path / "no-such-env")
    assert n == 0


def test_persist_env_preserves_existing_unrelated_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    env_file = fake_home / ".oxenclaw" / "env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        '# my custom config\nexport PROMPT="hi"\nexport OXENCLAW_LLAMACPP_BIN="/old/path"\n',
        encoding="utf-8",
    )

    from oxenclaw.flows.llamacpp_setup import _persist_env_lines

    _persist_env_lines(
        env_file,
        {"OXENCLAW_LLAMACPP_BIN": "/new/path", "OXENCLAW_LLAMACPP_GGUF": "/new/gguf"},
    )

    body = env_file.read_text(encoding="utf-8")
    # Unrelated content preserved.
    assert "PROMPT" in body
    # Old key was rewritten, not duplicated.
    assert body.count("OXENCLAW_LLAMACPP_BIN") == 1
    assert "/new/path" in body
    assert "/old/path" not in body
    # New key added.
    assert "OXENCLAW_LLAMACPP_GGUF" in body
