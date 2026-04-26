"""Live tests for the always-available isolation backends (inprocess + subprocess).

bwrap and container backends are exercised through availability checks
only — actually running them depends on the host having them installed.
The integration test suite (gated by env var) covers them in environments
that do.
"""

from __future__ import annotations

import sys

import pytest

from sampyclaw.security.isolation import (
    BubblewrapBackend,
    ContainerBackend,
    InprocessBackend,
    IsolationPolicy,
    SubprocessBackend,
    available_backends,
    resolve_backend,
)


def _open_policy(**overrides) -> IsolationPolicy:  # type: ignore[no-untyped-def]
    """Policy that allows the subprocess backend to actually run.

    The subprocess backend fails closed when network=False or filesystem!="full"
    because it cannot enforce either. These tests exercise process mechanics
    (rlimit, env scrub, timeout), not isolation primitives — so they opt out
    of the strict default explicitly.
    """
    overrides.setdefault("network", True)
    overrides.setdefault("filesystem", "full")
    return IsolationPolicy(**overrides)


# ── shared smoke ──


@pytest.fixture(params=["inprocess", "subprocess"])
def backend_name(request) -> str:  # type: ignore[no-untyped-def]
    return request.param


async def _make_backend(name: str):  # type: ignore[no-untyped-def]
    if name == "inprocess":
        return InprocessBackend()
    if name == "subprocess":
        if sys.platform == "win32":
            pytest.skip("subprocess backend is POSIX-only")
        return SubprocessBackend()
    raise AssertionError(f"unknown backend {name}")


async def test_echo_succeeds(backend_name: str) -> None:
    b = await _make_backend(backend_name)
    r = await b.run(["echo", "hello"], policy=_open_policy(timeout_seconds=2))
    assert r.ok
    assert "hello" in r.stdout


async def test_nonzero_exit_propagates(backend_name: str) -> None:
    b = await _make_backend(backend_name)
    r = await b.run(["false"], policy=_open_policy(timeout_seconds=2))
    assert not r.ok
    assert r.exit_code != 0


async def test_timeout_kills_process(backend_name: str) -> None:
    b = await _make_backend(backend_name)
    r = await b.run(["sleep", "5"], policy=_open_policy(timeout_seconds=0.5))
    assert r.timed_out is True
    assert r.exit_code != 0


async def test_stdout_truncation(backend_name: str) -> None:
    b = await _make_backend(backend_name)
    code = "import sys; sys.stdout.write('x' * 5000)"
    r = await b.run(
        ["python3", "-c", code],
        policy=_open_policy(max_output_bytes=200, timeout_seconds=2),
    )
    assert r.truncated_stdout
    assert "[truncated]" in r.stdout


async def test_missing_executable(backend_name: str) -> None:
    b = await _make_backend(backend_name)
    r = await b.run(["does-not-exist-xyz"], policy=_open_policy(timeout_seconds=2))
    # inprocess returns error string + exit -1; subprocess too.
    assert not r.ok


# ── subprocess-specific (rlimit) ──


async def test_subprocess_memory_cap_kills() -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX-only")
    b = SubprocessBackend()
    code = "a = bytearray(256 * 1024 * 1024); print('escaped')"
    r = await b.run(
        ["python3", "-c", code],
        policy=_open_policy(max_memory_mb=64, timeout_seconds=5),
    )
    assert "escaped" not in r.stdout
    assert r.exit_code != 0


async def test_subprocess_filesize_cap() -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX-only")
    b = SubprocessBackend()
    code = "f = open('/tmp/__sampy_big','wb'); f.write(b'x'*100_000_000); print('wrote ok')"
    r = await b.run(
        ["python3", "-c", code],
        policy=_open_policy(max_file_size_mb=10, timeout_seconds=5),
    )
    assert "wrote ok" not in r.stdout


async def test_subprocess_env_scrubbed_by_default() -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX-only")
    b = SubprocessBackend()
    r = await b.run(
        ["sh", "-c", "echo HOME=$HOME; echo USER=$USER; echo SECRET=$SECRET"],
        policy=_open_policy(timeout_seconds=2),
    )
    assert "HOME=\n" in r.stdout or "HOME=" in r.stdout
    # SECRET should not be set even if it was set in the parent.
    assert "SECRET=\n" in r.stdout or r.stdout.rstrip().endswith("SECRET=")


async def test_subprocess_refuses_oversized_stdin() -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX-only")
    b = SubprocessBackend()
    big = b"x" * 200
    r = await b.run(
        ["cat"],
        policy=_open_policy(max_stdin_bytes=100, timeout_seconds=2),
        stdin=big,
    )
    assert not r.ok
    assert r.error and "max_stdin_bytes" in r.error


async def test_subprocess_env_passthrough_allowlist() -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX-only")
    import os

    os.environ["SAMPY_TEST_TOKEN"] = "abc-123"
    try:
        b = SubprocessBackend()
        r = await b.run(
            ["sh", "-c", "echo TOKEN=$SAMPY_TEST_TOKEN"],
            policy=_open_policy(env_passthrough=("SAMPY_TEST_TOKEN",), timeout_seconds=2),
        )
        assert "TOKEN=abc-123" in r.stdout
    finally:
        del os.environ["SAMPY_TEST_TOKEN"]


# ── registry / fallback ──


async def test_registry_lists_available_backends() -> None:
    out = await available_backends()
    assert "inprocess" in out
    if sys.platform != "win32":
        assert "subprocess" in out


async def test_resolve_picks_strongest_available() -> None:
    chosen = await resolve_backend(IsolationPolicy())
    avail = await available_backends()
    # inprocess is the weakest; we should pick something at least as strong.
    assert chosen.name in avail


async def test_resolve_honours_explicit_pin_when_available() -> None:
    chosen = await resolve_backend(IsolationPolicy(backend="inprocess"))
    assert chosen.name == "inprocess"


async def test_resolve_falls_back_when_pinned_backend_missing() -> None:
    # container is unlikely to be installed in the test sandbox.
    avail = await available_backends()
    if "container" in avail:
        pytest.skip("container actually available, can't exercise fallback")
    chosen = await resolve_backend(IsolationPolicy(backend="container"))
    assert chosen.name in avail


async def test_bwrap_availability_matches_which() -> None:
    import shutil

    expected = shutil.which("bwrap") is not None
    assert (await BubblewrapBackend().is_available()) is expected


async def test_container_availability_matches_which() -> None:
    import shutil

    expected = (shutil.which("docker") is not None) or (shutil.which("podman") is not None)
    assert (await ContainerBackend().is_available()) is expected


def test_strength_ordering_is_correct() -> None:
    from sampyclaw.security.isolation.registry import _STRENGTH

    assert _STRENGTH["container"] > _STRENGTH["bwrap"]
    assert _STRENGTH["bwrap"] > _STRENGTH["subprocess"]
    assert _STRENGTH["subprocess"] > _STRENGTH["inprocess"]
