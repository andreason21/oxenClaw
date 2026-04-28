"""Tests for oxenclaw.memory.privacy (PII redaction) and related integration."""

from __future__ import annotations

import logging
from pathlib import Path

from oxenclaw.memory.privacy import DEFAULT_LEVEL, Redaction, redact
from oxenclaw.memory.walker import WalkerConfig, scan_memory_dir

# ── Unit: individual PII kinds ────────────────────────────────────────────────


def test_email_detected_light() -> None:
    text = "Contact me at user.name+tag@example.co.uk for details."
    cleaned, hits = redact(text, level="light")
    assert hits
    assert hits[0].kind == "email"
    assert "[REDACTED:email]" in cleaned
    assert "user.name+tag@example.co.uk" not in cleaned


def test_api_key_sk_detected_light() -> None:
    text = "Set OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456 in your env."
    cleaned, hits = redact(text, level="light")
    kinds = {h.kind for h in hits}
    assert "api_key" in kinds
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in cleaned


def test_api_key_gh_detected_light() -> None:
    text = "Token: ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234"
    cleaned, hits = redact(text, level="light")
    kinds = {h.kind for h in hits}
    assert "api_key" in kinds
    assert "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234" not in cleaned


def test_bearer_token_detected_light() -> None:
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"
    cleaned, hits = redact(text, level="light")
    kinds = {h.kind for h in hits}
    assert "bearer_token" in kinds
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in cleaned


def test_form_secret_password_detected_light() -> None:
    text = "Login with password=hunter2&username=admin"
    cleaned, hits = redact(text, level="light")
    kinds = {h.kind for h in hits}
    assert "form_secret" in kinds
    assert "hunter2" not in cleaned


def test_phone_only_in_strict() -> None:
    text = "Call me at +82 10-1234-5678 anytime."
    _, hits_light = redact(text, level="light")
    _, hits_strict = redact(text, level="strict")
    phone_light = [h for h in hits_light if h.kind == "phone"]
    phone_strict = [h for h in hits_strict if h.kind == "phone"]
    assert not phone_light, "phone should NOT be redacted in light mode"
    assert phone_strict, "phone SHOULD be redacted in strict mode"


def test_ipv4_only_in_strict() -> None:
    text = "Server address: 192.168.1.100"
    _, hits_light = redact(text, level="light")
    _, hits_strict = redact(text, level="strict")
    assert not any(h.kind == "ipv4" for h in hits_light)
    assert any(h.kind == "ipv4" for h in hits_strict)


def test_credit_card_luhn_valid_detected_strict() -> None:
    # Visa test number — Luhn valid
    text = "Card: 4532015112830366"
    _, hits = redact(text, level="strict")
    assert any(h.kind == "credit_card" for h in hits)


def test_credit_card_luhn_invalid_not_detected() -> None:
    # Luhn invalid (last digit off by 1)
    text = "Number: 4532015112830367"
    _, hits = redact(text, level="strict")
    assert not any(h.kind == "credit_card" for h in hits), (
        "Luhn-invalid number should not be flagged as credit card"
    )


def test_off_level_returns_unchanged() -> None:
    text = "My email is alice@example.com and key sk-abc12345678901234567890"
    cleaned, hits = redact(text, level="off")
    assert cleaned == text
    assert hits == []


def test_plain_text_no_hits() -> None:
    text = "The quick brown fox jumps over the lazy dog."
    cleaned, hits = redact(text, level="strict")
    assert cleaned == text
    assert hits == []


def test_default_level_is_light() -> None:
    assert DEFAULT_LEVEL == "light"


def test_redaction_record_fields() -> None:
    text = "email: dev@corp.io"
    _, hits = redact(text, level="light")
    assert hits
    r = hits[0]
    assert isinstance(r, Redaction)
    assert r.kind == "email"
    assert r.replacement == "[REDACTED:email]"
    start, end = r.span
    assert text[start:end] == "dev@corp.io"


# ── Integration: append_to_inbox + redaction ──────────────────────────────────


def test_inbox_redacts_secrets_and_logs_warning(tmp_path: Path, caplog) -> None:
    """append_to_inbox with redact_level='light' strips secrets and logs WARNING."""
    from oxenclaw.memory.inbox import append_to_inbox

    inbox = tmp_path / "inbox.md"
    text = "API key: sk-secretkey12345678901234 and user email@example.com"

    with caplog.at_level(logging.WARNING):
        append_to_inbox(inbox, text, redact_level="light")

    written = inbox.read_text()
    assert "sk-secretkey12345678901234" not in written
    assert "[REDACTED:api_key]" in written

    # Warning should have been emitted
    warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("redacted" in m and "secret" in m for m in warning_msgs)


def test_inbox_no_redact_by_default(tmp_path: Path) -> None:
    """Without redact_level, secrets pass through unchanged."""
    from oxenclaw.memory.inbox import append_to_inbox

    inbox = tmp_path / "inbox.md"
    text = "sk-verysecretkey1234567890abc123"
    append_to_inbox(inbox, text)
    assert "sk-verysecretkey1234567890abc123" in inbox.read_text()


# ── Walker: allow/deny glob filtering ────────────────────────────────────────


def test_walker_deny_glob_excludes_files(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("safe")
    (tmp_path / "secrets.md").write_text("secret")
    cfg = WalkerConfig(deny_globs=["secrets*"])
    rels = [r for r, *_ in scan_memory_dir(tmp_path, walker_config=cfg)]
    assert "notes.md" in rels
    assert "secrets.md" not in rels


def test_walker_allow_glob_limits_to_matching(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "b.md").write_text("b")
    sub = tmp_path / "notes"
    sub.mkdir()
    (sub / "c.md").write_text("c")
    cfg = WalkerConfig(allow_globs=["notes/*.md"])
    rels = [r for r, *_ in scan_memory_dir(tmp_path, walker_config=cfg)]
    assert rels == ["notes/c.md"]


def test_walker_deny_wins_over_allow(tmp_path: Path) -> None:
    """A file that matches both allow and deny should be excluded (deny wins)."""
    (tmp_path / "secrets.md").write_text("secret")
    cfg = WalkerConfig(allow_globs=["*.md"], deny_globs=["secrets*"])
    rels = [r for r, *_ in scan_memory_dir(tmp_path, walker_config=cfg)]
    assert "secrets.md" not in rels


def test_walker_min_size_drops_empty_files(tmp_path: Path) -> None:
    (tmp_path / "empty.md").write_text("")
    (tmp_path / "nonempty.md").write_text("content here")
    cfg = WalkerConfig(min_size=5)
    rels = [r for r, *_ in scan_memory_dir(tmp_path, walker_config=cfg)]
    assert "empty.md" not in rels
    assert "nonempty.md" in rels


def test_walker_max_size_from_walker_config(tmp_path: Path) -> None:
    (tmp_path / "small.md").write_text("ok")
    (tmp_path / "big.md").write_text("x" * 500)
    cfg = WalkerConfig(max_size=100)
    rels = [r for r, *_ in scan_memory_dir(tmp_path, walker_config=cfg)]
    assert "small.md" in rels
    assert "big.md" not in rels
