"""ClawHub-published skills (Clawdbot publisher) declare requires/install
under `metadata.clawdbot` instead of `metadata.openclaw`. The runtime
must treat the two as synonyms — otherwise `requires.bins:["uv"]` is
silently ignored, the compat filter reports "installable", and the
user's catalog/install flow misleads them onto skills that can't run
in their environment.

Locks the alias both at the parsing layer (`parse_skill_text`
promotes either to top-level `openclaw`) and at the compat-walker
layer (manifest dicts coming back from registry detail endpoints).
"""

from __future__ import annotations

from oxenclaw.clawhub.compat import check_skill_dict_compatibility
from oxenclaw.clawhub.frontmatter import parse_skill_text

_STOCK_ANALYSIS_SKILL_MD = """---
name: stock-analysis
description: Yahoo-Finance stock analysis with uv-managed deps.
version: 6.2.0
commands:
  - /stock - Analyze a stock or crypto
  - /stock_compare - Compare multiple tickers
metadata: {"clawdbot":{"emoji":"📈","requires":{"bins":["uv"],"env":[]},"install":[{"id":"uv-brew","kind":"brew","formula":"uv","bins":["uv"]}]}}
---

# body
"""


def test_clawdbot_metadata_promotes_to_openclaw_at_parse_time() -> None:
    """The exact failing manifest from production. After parse,
    `manifest.openclaw.requires.bins` must contain `"uv"` so the
    rest of the runtime can treat it identically to a skill that
    used the `openclaw` namespace."""
    manifest, _body = parse_skill_text(_STOCK_ANALYSIS_SKILL_MD)
    assert manifest.openclaw.emoji == "📈"
    assert manifest.openclaw.requires.bins == ["uv"]
    # Install hint round-trips so the dashboard's "missing dep" UI
    # surfaces it.
    assert len(manifest.openclaw.install) == 1
    assert manifest.openclaw.install[0].kind == "brew"
    assert manifest.openclaw.install[0].formula == "uv"


def test_compat_walker_reads_metadata_clawdbot() -> None:
    """Detail-endpoint payload still nests under `metadata.clawdbot`;
    the compat walker must drill in instead of returning "no
    constraints"."""
    payload = {
        "slug": "stock-analysis",
        "manifest": {
            "metadata": {
                "clawdbot": {
                    "os": ["darwin"],
                    "requires": {"bins": ["uv"]},
                }
            }
        },
    }
    # Run on Linux without uv installed.
    report = check_skill_dict_compatibility(
        payload,
        current_os="linux",
        which=lambda b: None,  # nothing on PATH
    )
    assert report.installable is False
    assert report.unsupported_os is True
    assert "uv" in report.missing_bins
    # Both reasons surface to the operator.
    assert any("darwin" in r for r in report.reasons)
    assert any("uv" in r for r in report.reasons)


def test_compat_walker_reads_metadata_clawdbot_at_top_level_summary() -> None:
    """Search-summary payloads sometimes carry the metadata block at
    the result root rather than under `manifest`."""
    payload = {
        "slug": "stock-analysis",
        "metadata": {"clawdbot": {"requires": {"bins": ["uv"]}}},
    }
    report = check_skill_dict_compatibility(
        payload,
        current_os="linux",
        which=lambda b: None,
    )
    assert report.installable is False
    assert report.missing_bins == ["uv"]


def test_openclaw_namespace_still_works() -> None:
    """Don't regress the original `metadata.openclaw` path."""
    md = """---
name: x
description: y
metadata: {"openclaw":{"requires":{"bins":["uv"]}}}
---
body
"""
    manifest, _ = parse_skill_text(md)
    assert manifest.openclaw.requires.bins == ["uv"]


def test_openclaw_wins_over_clawdbot_when_both_present() -> None:
    """If a manifest sets both, prefer `metadata.openclaw` — that's
    the canonical name and authors who set both meant it as the
    override."""
    md = """---
name: x
description: y
metadata:
  openclaw:
    emoji: O
  clawdbot:
    emoji: C
---
body
"""
    manifest, _ = parse_skill_text(md)
    assert manifest.openclaw.emoji == "O"
