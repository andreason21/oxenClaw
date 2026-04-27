"""Pure-Python PII redaction for the memory pipeline.

No external libraries required. Presidio is optional and not used here.

Usage::

    from oxenclaw.memory.privacy import redact, DEFAULT_LEVEL

    cleaned, hits = redact(text, level="light")
    for r in hits:
        print(r.kind, r.span)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# ── Public types ─────────────────────────────────────────────────────────────

RedactLevel = Literal["off", "light", "strict"]

DEFAULT_LEVEL: RedactLevel = "light"


@dataclass
class Redaction:
    """One PII match that was replaced."""

    span: tuple[int, int]  # (start, end) in the *original* string
    kind: str              # e.g. "email", "api_key", "phone", …
    replacement: str       # the text that was substituted


# ── Luhn helper ──────────────────────────────────────────────────────────────

def _luhn_check(digits: str) -> bool:
    """Return True if the digit string passes the Luhn algorithm."""
    total = 0
    reverse = digits[::-1]
    for i, ch in enumerate(reverse):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# ── Regex catalogue ──────────────────────────────────────────────────────────
#
# Each entry is (kind, pattern, levels_active).
#   levels_active = set of levels where this rule fires.
#   Order matters: longer/more-specific patterns first.

_RULES: list[tuple[str, re.Pattern[str], set[str]]] = []


def _r(kind: str, pattern: str, *, levels: set[str]) -> None:
    _RULES.append((kind, re.compile(pattern), levels))


# ── API / token patterns (light + strict) ────────────────────────────────────

# OpenAI / Anthropic sk- keys (≥20 chars after the prefix)
_r("api_key", r'\bsk-[A-Za-z0-9_\-]{20,}\b', levels={"light", "strict"})

# Slack tokens: xoxb- / xoxp- / xoxa- / xoxr-
_r("api_key", r'\bxox[bpar]-[A-Za-z0-9\-]{10,}\b', levels={"light", "strict"})

# GitHub personal-access-tokens: ghp_  ghs_
_r("api_key", r'\bgh[ps]_[A-Za-z0-9]{20,}\b', levels={"light", "strict"})

# AWS access key IDs
_r("api_key", r'\bAKIA[A-Z0-9]{16}\b', levels={"light", "strict"})

# Google OAuth tokens ya29.
_r("api_key", r'\bya29\.[A-Za-z0-9_\-]{20,}\b', levels={"light", "strict"})

# SSH public key prefix (whole line or inline)
_r("api_key", r'\bssh-rsa\s+AAAA[A-Za-z0-9+/=]{20,}\b', levels={"light", "strict"})

# Bearer tokens in Authorization: headers
_r(
    "bearer_token",
    r'(?i)Authorization\s*:\s*Bearer\s+([A-Za-z0-9\-._~+/=]{10,})',
    levels={"light", "strict"},
)

# form-encoded secrets: password=…  apikey=…  api_key=…  secret=…  token=…
# Matches key=value pairs wherever they appear (URL query string, env lines,
# plain prose, etc.). A word boundary before the key name prevents collisions
# with longer identifiers like "access_token_refresh".
_r(
    "form_secret",
    r'(?i)\b(?:password|api_?key|secret|token)\s*=\s*([^\s&;#\'"]{2,})',
    levels={"light", "strict"},
)

# Email addresses (light + strict)
_r(
    "email",
    r'\b[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,}\b',
    levels={"light", "strict"},
)

# ── Strict-only patterns ──────────────────────────────────────────────────────

# Phone numbers:
#   - Korean: 010-XXXX-XXXX / 02-XXX(X)-XXXX / 0XX-XXXX-XXXX
#   - International: +1 …  +44 …  +82 …
#   - Generic 10-digit US: (NXX) NXX-XXXX / NXX-NXX-XXXX
_r(
    "phone",
    r'(?:'
    # Korean mobile 010/011/016/017/018/019
    r'\b01[016789]-\d{3,4}-\d{4}\b'
    r'|'
    # Korean landline 02-xxx-xxxx or 02-xxxx-xxxx
    r'\b0[2-9]\d?-\d{3,4}-\d{4}\b'
    r'|'
    # International with + prefix
    r'\+\d{1,3}[\s\-.]?\(?\d{1,4}\)?[\s\-.]?\d{2,4}[\s\-.]?\d{2,4}[\s\-.]?\d{0,4}\b'
    r'|'
    # US 10-digit: (NXX) NXX-XXXX
    r'\(\d{3}\)\s*\d{3}[\s\-]\d{4}'
    r'|'
    # US plain: NXX-NXX-XXXX or NXX.NXX.XXXX
    r'\b\d{3}[\-\.]\d{3}[\-\.]\d{4}\b'
    r')',
    levels={"strict"},
)

# IPv4 addresses (strict only — too many false positives in light mode)
_r(
    "ipv4",
    r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b',
    levels={"strict"},
)

# Credit cards: 13-19 digits with common separators; validated via Luhn
# We do NOT add this directly — the CC handler below iterates manually so
# we can apply the Luhn filter.  It is defined here for documentation.
_CC_PATTERN = re.compile(
    r'\b(?:\d[ \-]?){12,18}\d\b'   # 13–19 digits, optional spaces/dashes between groups
)
_CC_LEVELS: set[str] = {"strict"}


# ── Core redact function ──────────────────────────────────────────────────────

def redact(
    text: str,
    *,
    level: RedactLevel = DEFAULT_LEVEL,
) -> tuple[str, list[Redaction]]:
    """Return ``(redacted_text, redaction_list)``.

    When ``level`` is ``"off"`` the original text is returned unchanged with
    an empty redaction list.
    """
    if level == "off":
        return text, []

    # Collect all match spans so we can de-overlap them.
    candidates: list[tuple[int, int, str]] = []  # (start, end, kind)

    # --- CC (with Luhn, strict only) ---
    if level in _CC_LEVELS:
        for m in _CC_PATTERN.finditer(text):
            digits = re.sub(r'[ \-]', '', m.group())
            if 13 <= len(digits) <= 19 and _luhn_check(digits):
                candidates.append((m.start(), m.end(), "credit_card"))

    # --- Regex-based rules ---
    for kind, pattern, levels in _RULES:
        if level not in levels:
            continue
        for m in pattern.finditer(text):
            candidates.append((m.start(), m.end(), kind))

    if not candidates:
        return text, []

    # Sort by start; break ties by preferring longer match (more specific).
    candidates.sort(key=lambda c: (c[0], -(c[1] - c[0])))

    # Remove overlapping spans (greedy, first match wins after sorting).
    merged: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, kind in candidates:
        if start < last_end:
            continue  # overlaps a previous accepted match
        merged.append((start, end, kind))
        last_end = end

    # Build output: walk through merged replacements.
    redactions: list[Redaction] = []
    result_parts: list[str] = []
    cursor = 0
    for start, end, kind in merged:
        result_parts.append(text[cursor:start])
        replacement = f"[REDACTED:{kind}]"
        result_parts.append(replacement)
        redactions.append(Redaction(span=(start, end), kind=kind, replacement=replacement))
        cursor = end
    result_parts.append(text[cursor:])
    return "".join(result_parts), redactions
