"""Static analysis for SKILL.md content + manifest install specs.

Skills themselves are markdown — no code execution path inside the gateway.
But a skill body can *instruct the user* to do something dangerous (`curl … | sh`,
`rm -rf /`, dump-env-to-server) and the agent might propagate that instruction.
Manifest install specs can also point at fishy URLs/formulas.

This scanner catches the most common red flags. It is **deliberately
conservative** — false positives are fine; false negatives are not.

Mirrors the surface of openclaw `src/security/skill-scanner.ts`. Categories
overlap intentionally; severity reflects how bad a real instance would be.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sampyclaw.clawhub.frontmatter import SkillManifest


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: Severity
    message: str
    location: str  # "body" or "manifest.install[i]" etc.
    snippet: str | None = None


@dataclass(frozen=True)
class _Rule:
    id: str
    severity: Severity
    pattern: re.Pattern[str]
    message: str


_BODY_RULES: tuple[_Rule, ...] = (
    _Rule(
        id="curl-pipe-shell",
        severity=Severity.CRITICAL,
        pattern=re.compile(
            r"\b(curl|wget|fetch)\b[^\n|]*\|\s*(sh|bash|zsh|fish|dash)\b",
            re.IGNORECASE,
        ),
        message="Skill instructs user to pipe a remote download into a shell — classic install-trojan pattern.",
    ),
    _Rule(
        id="dangerous-rm",
        severity=Severity.CRITICAL,
        pattern=re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+/\s*(?:$|\s)"),
        message="Skill body contains `rm -rf /` (or close variant).",
    ),
    _Rule(
        id="env-exfiltration",
        severity=Severity.WARN,
        pattern=re.compile(
            r"(printenv|env\s|process\.env|\$\{?[A-Z_]+\}?)\s*[|>].*?(curl|wget|nc|ncat)",
            re.IGNORECASE,
        ),
        message="Possible env-variable exfiltration to a remote endpoint.",
    ),
    _Rule(
        id="dynamic-eval",
        severity=Severity.WARN,
        pattern=re.compile(r"\b(eval|exec|Function\s*\(|setTimeout\s*\(\s*['\"]|`\$\(.+\)`)\b"),
        message="Skill body references dynamic-code-execution constructs.",
    ),
    _Rule(
        id="suspicious-base64-blob",
        severity=Severity.WARN,
        pattern=re.compile(r"[A-Za-z0-9+/]{200,}={0,2}"),
        message="Long base64-looking blob in skill body.",
    ),
    _Rule(
        id="hex-blob",
        severity=Severity.INFO,
        pattern=re.compile(r"(?:\\x[0-9a-fA-F]{2}){32,}"),
        message="Long hex-encoded blob in skill body.",
    ),
    _Rule(
        id="sudo-rm-or-chmod",
        severity=Severity.WARN,
        pattern=re.compile(r"\bsudo\s+(rm|chmod\s+\d*7|chown)\b"),
        message="Skill body uses `sudo` against rm/chmod/chown — review carefully.",
    ),
    _Rule(
        id="ssh-key-harvest",
        severity=Severity.CRITICAL,
        pattern=re.compile(r"~/\.ssh/(id_[a-zA-Z0-9_]+|authorized_keys|config)", re.IGNORECASE),
        message="Skill references private SSH keys or authorized_keys.",
    ),
    _Rule(
        id="aws-credential-harvest",
        severity=Severity.CRITICAL,
        pattern=re.compile(r"~/\.(aws/credentials|kube/config|docker/config\.json)", re.IGNORECASE),
        message="Skill references cloud credentials files.",
    ),
    _Rule(
        id="reverse-shell",
        severity=Severity.CRITICAL,
        pattern=re.compile(
            r"\b(bash\s+-i\s+>&\s*/dev/tcp|nc\s+-e|ncat\s+--exec|python\s+.*?socket\.socket\(.+?connect)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        message="Reverse-shell-shaped command in skill body.",
    ),
)


_SAFE_INSTALL_KINDS = {"brew", "node", "go", "uv", "download"}


def _scan_install_specs(manifest: SkillManifest) -> Iterable[Finding]:
    for idx, spec in enumerate(manifest.openclaw.install):
        location = f"manifest.install[{idx}]"
        kind = spec.kind
        if kind not in _SAFE_INSTALL_KINDS:
            yield Finding(
                rule="install-kind-unknown",
                severity=Severity.WARN,
                message=f"Unfamiliar install kind {kind!r}; review before allowing.",
                location=location,
            )
        url = spec.url
        if url:
            if not url.startswith("https://"):
                yield Finding(
                    rule="install-url-not-https",
                    severity=Severity.CRITICAL,
                    message=f"Install URL is not HTTPS: {url}",
                    location=location,
                    snippet=url,
                )
            if re.search(r"\b(\d{1,3}\.){3}\d{1,3}\b", url):
                yield Finding(
                    rule="install-url-raw-ip",
                    severity=Severity.WARN,
                    message="Install URL points at a raw IP address.",
                    location=location,
                    snippet=url,
                )
        if spec.formula and "/" not in spec.formula and kind == "brew":
            yield Finding(
                rule="brew-bare-formula",
                severity=Severity.INFO,
                message=f"Brew formula {spec.formula!r} has no tap — installs from homebrew-core.",
                location=location,
            )


def _scan_body(body: str) -> Iterable[Finding]:
    for rule in _BODY_RULES:
        for match in rule.pattern.finditer(body):
            yield Finding(
                rule=rule.id,
                severity=rule.severity,
                message=rule.message,
                location="body",
                snippet=match.group(0)[:200],
            )


class SkillScanner:
    """Run all rules against a manifest + body and bucket the findings."""

    def scan(self, manifest: SkillManifest, body: str) -> list[Finding]:
        return [*_scan_install_specs(manifest), *_scan_body(body)]

    def has_critical(self, findings: list[Finding]) -> bool:
        return any(f.severity is Severity.CRITICAL for f in findings)

    def summarise(self, findings: list[Finding]) -> dict[str, int]:
        out = {s.value: 0 for s in Severity}
        for f in findings:
            out[f.severity.value] += 1
        return out


def scan_skill(manifest: SkillManifest, body: str) -> list[Finding]:
    return SkillScanner().scan(manifest, body)
