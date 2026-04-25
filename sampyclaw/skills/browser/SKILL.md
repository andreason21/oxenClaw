---
name: browser
version: 0.1.0
description: |
  Drive a sandboxed headless Chromium to read pages, take screenshots,
  click links, and fill forms. Every request runs through sampyClaw's
  net policy (host allowlist + DNS pinning + IP classification), so the
  browser cannot reach hosts the operator has not approved.
gated: true
requires:
  optional_extras: [browser]
  env_marker: SAMPYCLAW_ENABLE_BROWSER
notes:
  - "Disabled by default. Set SAMPYCLAW_ENABLE_BROWSER=1 (or config.yaml browser.enabled: true) and provide an allowlist via SAMPYCLAW_NET_ALLOW_HOSTS."
  - "Run `playwright install chromium` once after `pip install 'sampyclaw[browser]'`."
  - "Downloads + browser_evaluate are NOT bundled by default — add them explicitly when you understand the risk."
tools:
  - browser_navigate
  - browser_snapshot
  - browser_screenshot
  - browser_click
  - browser_fill
---

# Browser skill

Use the `browser_*` tools to fetch and interact with web pages. The browser
runs headless Chromium in a fresh ephemeral context per call, so cookies
and storage do not survive across calls. The browser refuses to navigate
to any URL whose host is not in `SAMPYCLAW_NET_ALLOW_HOSTS` (or the
operator-configured `BrowserPolicy`); it also refuses loopback,
RFC1918/CGNAT, and IPv6 link-local destinations unless those are
explicitly allowed.

## When to use it

- Reading a public web page that the model needs to summarise.
- Taking a screenshot of a public dashboard for visual verification.
- Driving a single click/fill to advance a public sign-in or form.

## When **not** to use it

- Anything the operator has not pre-approved via the host allowlist.
- Bulk scraping (no rate-limiter is built in; use a dedicated crawler).
- Authenticated flows that require persistent cookies (the default
  context is wiped after each call).
