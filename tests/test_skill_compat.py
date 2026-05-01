"""Skill compatibility checker.

Filters catalog entries that obviously cannot install/run on the
current machine (wrong OS, missing CLI binary, missing required env
var) so the dashboard's Skills view doesn't drown the operator in
mac-only or API-key-gated skills they can't use.
"""

from __future__ import annotations

from oxenclaw.clawhub.compat import (
    CompatibilityReport,
    check_compatibility,
    check_skill_dict_compatibility,
)

# Stubs so tests don't depend on the host machine's actual PATH/env.


def _which_factory(present: set[str]):
    def _w(b: str) -> str | None:
        return f"/usr/bin/{b}" if b in present else None

    return _w


def _env(present: dict[str, str] | None = None):
    return present or {}


# ─── check_compatibility primitive ────────────────────────────────────


def test_no_constraints_is_installable() -> None:
    r = check_compatibility(current_os="linux", which=_which_factory(set()))
    assert r.installable is True
    assert r.reasons == []


def test_os_mismatch_blocks() -> None:
    r = check_compatibility(
        os_list=["darwin"],
        current_os="linux",
        which=_which_factory(set()),
    )
    assert r.installable is False
    assert r.unsupported_os is True
    assert any("darwin" in s for s in r.reasons)


def test_os_synonym_macos_matches_darwin() -> None:
    """Manifest authors write `macos`, sys.platform is `darwin` —
    treat them as the same."""
    r = check_compatibility(os_list=["macos"], current_os="darwin", which=_which_factory(set()))
    assert r.installable is True


def test_missing_required_bins_blocks() -> None:
    r = check_compatibility(
        requires_bins=["foo", "bar"],
        current_os="linux",
        which=_which_factory({"foo"}),
    )
    assert r.installable is False
    assert r.missing_bins == ["bar"]


def test_required_any_bins_one_present_passes() -> None:
    r = check_compatibility(
        requires_any_bins=["python3", "python"],
        current_os="linux",
        which=_which_factory({"python3"}),
    )
    assert r.installable is True
    assert r.missing_any_bins == []


def test_required_any_bins_none_present_blocks() -> None:
    r = check_compatibility(
        requires_any_bins=["foo", "bar"],
        current_os="linux",
        which=_which_factory(set()),
    )
    assert r.installable is False
    assert set(r.missing_any_bins) == {"foo", "bar"}


def test_missing_required_env_blocks() -> None:
    r = check_compatibility(
        requires_env=["OPENAI_API_KEY", "DATABASE_URL"],
        current_os="linux",
        which=_which_factory(set()),
        environ={"OPENAI_API_KEY": "sk-x"},
    )
    assert r.installable is False
    assert r.missing_env == ["DATABASE_URL"]


def test_empty_string_env_value_treated_as_missing() -> None:
    r = check_compatibility(
        requires_env=["KEY"],
        current_os="linux",
        which=_which_factory(set()),
        environ={"KEY": ""},
    )
    assert r.installable is False
    assert r.missing_env == ["KEY"]


def test_multiple_failure_reasons_accumulate() -> None:
    r = check_compatibility(
        os_list=["darwin"],
        requires_bins=["foo"],
        requires_env=["KEY"],
        current_os="linux",
        which=_which_factory(set()),
        environ={},
    )
    assert r.installable is False
    assert r.unsupported_os is True
    assert r.missing_bins == ["foo"]
    assert r.missing_env == ["KEY"]
    assert len(r.reasons) == 3  # one reason per constraint kind


# ─── check_skill_dict_compatibility (manifest shape walker) ───────────


def test_summary_with_no_metadata_passes_by_default() -> None:
    """Search summaries often only have `slug` + `displayName` — we
    can't prove incompatibility, so we don't hide them."""
    r = check_skill_dict_compatibility(
        {"slug": "foo", "displayName": "Foo"},
        current_os="linux",
        which=_which_factory(set()),
    )
    assert r.installable is True


def test_walker_handles_detail_versioned_shape() -> None:
    """`fetch_skill_detail` returns nested `latestVersion.manifest.openclaw`."""
    payload = {
        "slug": "stock-watcher",
        "latestVersion": {
            "manifest": {
                "openclaw": {
                    "os": ["darwin"],
                    "requires": {"bins": ["yfinance-cli"]},
                }
            }
        },
    }
    r = check_skill_dict_compatibility(payload, current_os="linux", which=_which_factory(set()))
    assert r.installable is False
    assert r.unsupported_os is True
    assert "yfinance-cli" in r.missing_bins


def test_walker_handles_metadata_openclaw_shape() -> None:
    """SKILL.md frontmatter often nests `metadata.openclaw.…`."""
    payload = {
        "manifest": {
            "metadata": {
                "openclaw": {
                    "os": ["linux"],
                    "requires": {"env": ["API_KEY"]},
                }
            }
        }
    }
    r = check_skill_dict_compatibility(
        payload,
        current_os="linux",
        which=_which_factory(set()),
        environ={},
    )
    assert r.installable is False
    assert r.missing_env == ["API_KEY"]


def test_walker_handles_top_level_openclaw() -> None:
    payload = {"openclaw": {"os": ["windows"]}}
    r = check_skill_dict_compatibility(payload, current_os="linux", which=_which_factory(set()))
    assert r.installable is False


def test_walker_supports_anyBins_camel_and_snake() -> None:
    """Real registries use both `anyBins` (clawhub TS naming) and
    `any_bins` (snake variant). Both must work."""
    for key in ("anyBins", "any_bins"):
        payload = {"openclaw": {"requires": {key: ["aaa", "bbb"]}}}
        r = check_skill_dict_compatibility(payload, current_os="linux", which=_which_factory(set()))
        assert r.installable is False, key
        assert set(r.missing_any_bins) == {"aaa", "bbb"}


def test_to_dict_round_trip() -> None:
    r = CompatibilityReport(
        installable=False,
        reasons=["no foo"],
        missing_bins=["foo"],
        unsupported_os=True,
        current_os="linux",
    )
    d = r.to_dict()
    assert d["installable"] is False
    assert d["missing_bins"] == ["foo"]
    assert d["current_os"] == "linux"
