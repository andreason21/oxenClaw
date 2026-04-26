# Browser tools (BR-1)

oxenClaw ships a sandboxed Playwright-backed browser surface for the
LLM. The design goal: **let an agent read public pages without giving
it a way to exfiltrate data to anywhere the operator has not approved.**

## Why a thin wrapper, not a 1:1 port of `extensions/browser/`

openclaw's `extensions/browser/` is ~24K LOC across 156 files and
includes a CDP bridge + control plane + qa-lab harness. The
*security-relevant* surface is ~700 LOC across 6 files
(`navigation-guard.ts`, `request-policy.ts`, `cdp-reachability-policy.ts`,
`ssrf-policy-helpers.ts`, plus the SDK-side `browser-security-runtime`
helpers). oxenClaw already has the equivalent of all of those in
`oxenclaw/security/net/`.

So BR-1 is a thin layer that:

1. Reuses the existing `NetPolicy` / `assert_url_allowed` / `assert_ip_allowed`.
2. Adds a Playwright lifecycle (`PlaywrightSession`).
3. Adds **one** new layer — `egress.build_route_handler` — that proxies
   every browser request through the existing net checks.
4. Adds **one** browser-specific cache — `pinning.HostPinCache` — for
   per-host DNS pinning at the route-handler layer.

## Performance choices

- **One Chromium per process.** Launch (`~500 ms`) is the expensive
  step; we share the browser across every skill that needs it.
- **One BrowserContext per call.** Contexts cost ~5 ms and provide full
  isolation (separate cookies, storage, cache, service workers, route
  handler). No cross-skill leakage.
- **Pinning cache is in-memory + lock-free on hits.** A hot host hits
  the LRU in ~1 µs; cold misses do one `getaddrinfo` (~1 ms). Cache is
  capacity-bounded so a long-running session can't grow unbounded.
- **Audit is opt-in.** When `OXENCLAW_AUDIT_OUTBOUND=1` the route
  handler writes to the same WAL sqlite store as the existing
  `aiohttp` audit layer. When the env is off, the handler does no
  sqlite work — it stays cache-bound.

## Layered egress controls

Every browser request is filtered by **four** independent layers; if
any one of them refuses, the request is aborted before a packet leaves
the host.

| Layer | Where it lives | What it catches |
|---|---|---|
| L1: scheme / port / host pattern | `security/net/ssrf.assert_url_allowed` | `http://` when policy is `https`-only, non-allowlisted hosts, IP-literals (loopback / RFC1918 / link-local) at preflight |
| L2: per-request policy preflight | `browser/egress.build_route_handler` | Re-validates *every* sub-resource, fetch, XHR, navigation redirect — not just the top-level URL |
| L3: DNS pinning | `browser/pinning.HostPinCache` | DNS rebinding (public IP at preflight → private IP at fetch time); a host whose IP set goes fully disjoint mid-session is refused |
| L4: dead proxy | `--proxy-server=http://0.0.0.0:1` Chromium flag | If anything escapes L1-L3 (e.g. a service-worker bootstrap path Playwright doesn't surface), the request still hits a proxy with no listener and dies at the network layer |

## Closed-by-default

`BrowserPolicy.closed()` (the default for the bundled tools) refuses
everything until extended:

- `allowed_schemes = ("https",)` — no plain HTTP.
- `allowed_hostnames = ()` plus `is_hostname_allowed` only returns True
  for the explicit allowlist when the allowlist is non-empty (so
  every navigation needs `policy.with_extra_allowed_hosts(...)`).
- `allow_loopback = False`, `allow_private_network = False`.
- `allow_websockets = False`, `allow_downloads = False`.
- Per-call ephemeral context (no persistent cookies/storage).

Operators opt in via env (`OXENCLAW_NET_ALLOW_HOSTS=example.com,*.docs.io`)
or by passing a custom `BrowserPolicy` to `default_browser_tools(policy=...)`.

## Tools

The `default_browser_tools(...)` bundle exposes five always-safe tools.
`browser_evaluate` and `browser_download` are **not** bundled — register
them explicitly when the use case justifies the risk.

| Tool | What it does | Output cap |
|---|---|---|
| `browser_navigate` | Open URL, return final URL + status + title | n/a |
| `browser_snapshot` | DOM as text / HTML / ARIA tree | `policy.max_dom_chars` (80 KiB) |
| `browser_screenshot` | PNG screenshot, base64 data URI | `policy.max_screenshot_bytes` (2 MiB) |
| `browser_click` | Navigate then click selector | n/a |
| `browser_fill` | Navigate then fill input | n/a |
| `browser_evaluate` (opt-in) | Run JS expression in page context | `policy.max_eval_chars` (20 KiB) |
| `browser_download` (opt-in, requires `allow_downloads`) | Save a triggered download | `policy.max_download_bytes` (8 MiB) |

## Install

The `playwright` Python SDK is an optional extra; default installs stay
slim and CI does not need Chromium.

```bash
pip install 'oxenclaw[browser]'
playwright install chromium
```

## Enabling at the gateway

```bash
export OXENCLAW_ENABLE_BROWSER=1
export OXENCLAW_NET_ALLOW_HOSTS='example.com,*.docs.io'
oxenclaw gateway start --provider pi --auth-token "$TOKEN"
```

`agents.factory._maybe_browser_tools()` reads those env vars on agent
construction and registers the bundle when both are set.

## What we deliberately did not port

- **CDP bridge / control plane** (openclaw `bridge-server.ts`,
  `control-auth*.ts`) — that's openclaw's remote-CDP product surface,
  not the LLM tool surface.
- **`chrome-mcp.*`** — exposing the browser as an MCP server. Out of
  scope for in-process Python tools.
- **Profile decoration / persistent profiles** — Playwright's stock
  Chromium does what we need; persistence is opt-in via
  `BrowserPolicy.persistent_profile_dir` only.
- **`qa-lab`, `diffs`** — separate openclaw extensions, not blockers.
