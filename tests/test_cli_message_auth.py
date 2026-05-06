"""Tests for `oxenclaw message {send,agents}` bearer-token wiring.

The gateway gates WS upgrades on `Authorization: Bearer <token>` (or a
`?token=` query, or the `oxenclaw_token` cookie). Before this fix the
CLI called `connect(gateway)` with no auth, so any token-protected
gateway returned HTTP 401 and the user had to manually splice a query
string into the URL.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from oxenclaw.cli import message_cmd
from oxenclaw.cli.__main__ import app

runner = CliRunner()


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __aenter__(self) -> "_FakeWS":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"status": "ok"}})


@pytest.fixture()
def captured_connect(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    fake_ws = _FakeWS()

    def _fake_connect(url: str, **kwargs: Any) -> _FakeWS:
        captured["url"] = url
        captured["kwargs"] = kwargs
        captured["ws"] = fake_ws
        return fake_ws

    monkeypatch.setattr(message_cmd, "connect", _fake_connect)
    monkeypatch.delenv("OXENCLAW_GATEWAY_TOKEN", raising=False)
    return captured


def test_send_passes_explicit_token_as_bearer(captured_connect: dict[str, Any]) -> None:
    result = runner.invoke(
        app,
        [
            "message",
            "send",
            "hello",
            "--chat-id",
            "c1",
            "--auth-token",
            "tok-explicit",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured_connect["kwargs"]["additional_headers"] == {
        "Authorization": "Bearer tok-explicit"
    }


def test_send_falls_back_to_env_token(
    captured_connect: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OXENCLAW_GATEWAY_TOKEN", "tok-from-env")
    result = runner.invoke(app, ["message", "send", "hi", "--chat-id", "c2"])
    assert result.exit_code == 0, result.output
    assert captured_connect["kwargs"]["additional_headers"] == {
        "Authorization": "Bearer tok-from-env"
    }


def test_send_omits_auth_header_when_no_token(captured_connect: dict[str, Any]) -> None:
    result = runner.invoke(app, ["message", "send", "hi", "--chat-id", "c3"])
    assert result.exit_code == 0, result.output
    assert captured_connect["kwargs"] == {}


def test_agents_passes_bearer_token(captured_connect: dict[str, Any]) -> None:
    result = runner.invoke(
        app, ["message", "agents", "--auth-token", "tok-a"]
    )
    assert result.exit_code == 0, result.output
    assert captured_connect["kwargs"]["additional_headers"] == {
        "Authorization": "Bearer tok-a"
    }
