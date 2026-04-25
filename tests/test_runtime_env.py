"""Tests for sampyclaw.plugin_sdk.runtime_env helpers."""

from __future__ import annotations

from unittest.mock import patch

from sampyclaw.plugin_sdk.runtime_env import (
    describe_platform,
    get_logger,
    is_wsl,
)


def test_get_logger_returns_namespaced_logger():
    logger = get_logger("test.runtime_env")
    assert logger.name == "sampyclaw.test.runtime_env"


def test_is_wsl_detects_microsoft_kernel():
    with patch("sys.platform", "linux"), patch(
        "platform.release", return_value="5.15.90.1-microsoft-standard-WSL2"
    ):
        assert is_wsl() is True


def test_is_wsl_detects_wsl_marker():
    with patch("sys.platform", "linux"), patch(
        "platform.release", return_value="5.15.0-WSL2"
    ):
        assert is_wsl() is True


def test_is_wsl_false_on_native_linux():
    with patch("sys.platform", "linux"), patch(
        "platform.release", return_value="6.5.0-generic"
    ), patch("pathlib.Path.exists", return_value=False):
        assert is_wsl() is False


def test_is_wsl_false_on_macos():
    with patch("sys.platform", "darwin"), patch(
        "platform.release", return_value="22.6.0"
    ):
        assert is_wsl() is False


def test_is_wsl_false_on_windows_native():
    with patch("sys.platform", "win32"):
        assert is_wsl() is False


def test_describe_platform_appends_wsl_marker():
    with patch("sys.platform", "linux"), patch(
        "platform.system", return_value="Linux"
    ), patch(
        "platform.release", return_value="5.15.0-microsoft-standard-WSL2"
    ):
        assert "WSL2" in describe_platform()


def test_describe_platform_no_wsl_marker_on_native():
    with patch("sys.platform", "linux"), patch(
        "platform.system", return_value="Linux"
    ), patch(
        "platform.release", return_value="6.5.0-generic"
    ), patch("pathlib.Path.exists", return_value=False):
        assert "WSL" not in describe_platform()
