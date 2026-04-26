"""Tests for `sampyclaw.config.auth_token`."""

from __future__ import annotations

from pathlib import Path

from sampyclaw.config.auth_token import (
    TOKEN_FILE_NAME,
    format_startup_banner,
    generate_token,
    load_persisted_token,
    resolve_or_generate_token,
    token_file_path,
    write_persisted_token,
)
from sampyclaw.config.paths import SampyclawPaths


def _paths(tmp_path: Path) -> SampyclawPaths:
    return SampyclawPaths(home=tmp_path)


def test_generate_token_is_48_hex_chars():
    a = generate_token()
    b = generate_token()
    assert len(a) == 48
    assert a != b
    int(a, 16)  # valid hex


def test_token_file_path_under_home(tmp_path: Path):
    paths = _paths(tmp_path)
    assert token_file_path(paths) == tmp_path / TOKEN_FILE_NAME


def test_load_persisted_returns_none_when_missing(tmp_path: Path):
    paths = _paths(tmp_path)
    assert load_persisted_token(paths) is None


def test_write_then_load_round_trip(tmp_path: Path):
    paths = _paths(tmp_path)
    p = write_persisted_token("abcd1234", paths)
    assert p == tmp_path / TOKEN_FILE_NAME
    loaded = load_persisted_token(paths)
    assert loaded is not None
    token, path = loaded
    assert token == "abcd1234"
    assert path == p


def test_write_uses_mode_0600_when_supported(tmp_path: Path):
    import os
    import stat

    paths = _paths(tmp_path)
    p = write_persisted_token("topsecret", paths)
    if os.name == "posix":
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == 0o600


def test_resolve_explicit_token_wins(tmp_path: Path):
    paths = _paths(tmp_path)
    write_persisted_token("from-file", paths)
    resolved = resolve_or_generate_token(
        explicit="from-cli",
        paths=paths,
        env={"SAMPYCLAW_GATEWAY_TOKEN": "from-env"},
    )
    assert resolved.token == "from-cli"
    assert resolved.source == "explicit"


def test_resolve_env_beats_persisted(tmp_path: Path):
    paths = _paths(tmp_path)
    write_persisted_token("from-file", paths)
    resolved = resolve_or_generate_token(paths=paths, env={"SAMPYCLAW_GATEWAY_TOKEN": "from-env"})
    assert resolved.token == "from-env"
    assert resolved.source == "env"


def test_resolve_loads_persisted_when_no_explicit_or_env(tmp_path: Path):
    paths = _paths(tmp_path)
    write_persisted_token("from-file", paths)
    resolved = resolve_or_generate_token(paths=paths, env={})
    assert resolved.token == "from-file"
    assert resolved.source == "persisted"
    assert resolved.path == tmp_path / TOKEN_FILE_NAME


def test_resolve_generates_and_persists_when_missing(tmp_path: Path):
    paths = _paths(tmp_path)
    resolved = resolve_or_generate_token(paths=paths, env={})
    assert resolved.source == "generated"
    assert resolved.path is not None and resolved.path.exists()
    # The just-written file holds the same token.
    loaded = load_persisted_token(paths)
    assert loaded is not None
    assert loaded[0] == resolved.token


def test_resolve_rotate_replaces_persisted(tmp_path: Path):
    paths = _paths(tmp_path)
    write_persisted_token("old-token", paths)
    resolved = resolve_or_generate_token(paths=paths, env={}, rotate=True)
    assert resolved.source == "generated"
    assert resolved.token != "old-token"
    loaded = load_persisted_token(paths)
    assert loaded is not None and loaded[0] == resolved.token


def test_resolve_empty_env_var_is_treated_as_unset(tmp_path: Path):
    paths = _paths(tmp_path)
    write_persisted_token("from-file", paths)
    resolved = resolve_or_generate_token(paths=paths, env={"SAMPYCLAW_GATEWAY_TOKEN": "   "})
    assert resolved.source == "persisted"


def test_format_startup_banner_for_generated_includes_token_and_url():
    from sampyclaw.config.auth_token import ResolvedToken

    r = ResolvedToken(token="ABC123", source="generated", path=Path("/x/gateway-token"))
    text = format_startup_banner(r, host="127.0.0.1", port=7331)
    assert "ABC123" in text
    assert "http://127.0.0.1:7331/?token=ABC123" in text
    assert "/x/gateway-token" in text


def test_format_startup_banner_for_persisted_includes_rotate_hint():
    from sampyclaw.config.auth_token import ResolvedToken

    r = ResolvedToken(token="XYZ", source="persisted", path=Path("/x/gateway-token"))
    text = format_startup_banner(r, host="h", port=1)
    assert "XYZ" in text
    assert "rotate" in text


def test_format_startup_banner_env_does_not_print_secret():
    from sampyclaw.config.auth_token import ResolvedToken

    r = ResolvedToken(token="SECRET-FROM-ENV", source="env")
    text = format_startup_banner(r, host="h", port=1)
    assert "SECRET-FROM-ENV" not in text
    assert "SAMPYCLAW_GATEWAY_TOKEN" in text


def test_format_startup_banner_explicit_does_not_print_secret():
    from sampyclaw.config.auth_token import ResolvedToken

    r = ResolvedToken(token="SECRET-FROM-CLI", source="explicit")
    text = format_startup_banner(r, host="h", port=1)
    assert "SECRET-FROM-CLI" not in text
    assert "--auth-token" in text
