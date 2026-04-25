"""Unit tests for sampyclaw.browser.policy."""

from __future__ import annotations

import pytest

from sampyclaw.browser.policy import (
    ABSOLUTE_MAX_DOM_CHARS,
    ABSOLUTE_MAX_PAGES,
    BrowserPolicy,
    merge_browser_policies,
)
from sampyclaw.security.net.policy import NetPolicy


def test_default_is_fully_closed() -> None:
    pol = BrowserPolicy.closed()
    assert pol.net.allowed_hostnames == ()
    assert pol.net.allow_loopback is False
    assert pol.net.allow_private_network is False
    assert pol.net.allowed_schemes == ("https",)
    assert pol.allow_downloads is False
    assert pol.allow_websockets is False


def test_default_constructor_uses_default_net() -> None:
    pol = BrowserPolicy()
    assert pol.net.allowed_schemes == ("http", "https")  # NetPolicy default
    assert pol.max_concurrent_pages == 4


def test_caps_refused_when_above_absolute() -> None:
    with pytest.raises(ValueError):
        BrowserPolicy(max_concurrent_pages=ABSOLUTE_MAX_PAGES + 1)
    with pytest.raises(ValueError):
        BrowserPolicy(max_dom_chars=ABSOLUTE_MAX_DOM_CHARS + 1)
    with pytest.raises(ValueError):
        BrowserPolicy(max_concurrent_pages=0)


def test_merge_takes_min_of_caps_and_and_of_flags() -> None:
    a = BrowserPolicy(max_concurrent_pages=4, allow_downloads=True, allow_websockets=False)
    b = BrowserPolicy(max_concurrent_pages=2, allow_downloads=True, allow_websockets=True)
    merged = merge_browser_policies(a, b)
    assert merged.max_concurrent_pages == 2
    assert merged.allow_downloads is True
    assert merged.allow_websockets is False  # AND


def test_merge_intersects_hostnames() -> None:
    a = BrowserPolicy(net=NetPolicy(allowed_hostnames=("a.example.com", "b.example.com")))
    b = BrowserPolicy(net=NetPolicy(allowed_hostnames=("b.example.com", "c.example.com")))
    merged = merge_browser_policies(a, b)
    assert merged.net.allowed_hostnames == ("b.example.com",)


def test_with_extra_allowed_hosts() -> None:
    pol = BrowserPolicy.closed().with_extra_allowed_hosts("example.com")
    assert "example.com" in pol.net.allowed_hostnames


def test_from_env_round_trip() -> None:
    env = {
        "SAMPYCLAW_NET_ALLOW_HOSTS": "example.com,*.test.io",
        "SAMPYCLAW_BROWSER_MAX_PAGES": "2",
        "SAMPYCLAW_BROWSER_ALLOW_DOWNLOADS": "1",
    }
    pol = BrowserPolicy.from_env(env)
    assert pol.net.allowed_hostnames == ("example.com", "*.test.io")
    assert pol.max_concurrent_pages == 2
    assert pol.allow_downloads is True


def test_merge_returns_first_when_single() -> None:
    a = BrowserPolicy(max_concurrent_pages=2)
    assert merge_browser_policies(a) is a


def test_merge_empty_returns_default() -> None:
    pol = merge_browser_policies()
    assert pol.max_concurrent_pages == BrowserPolicy().max_concurrent_pages
