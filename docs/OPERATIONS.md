# sampyClaw Operations Guide

How to install, run, observe, back up, and recover a sampyClaw deployment.
This guide assumes you have already gone through `README.md` for the
project background and `docs/SECURITY.md` for the threat model.

---

## Install

```bash
pip install -e ".[dev]"
sampyclaw paths              # confirm $HOME/.sampyclaw layout
sampyclaw config validate    # validate config.yaml + mcp.json + creds + env refs
```

Set credentials and config under `~/.sampyclaw/`:

```
~/.sampyclaw/
├── config.yaml                       # channel & agent config
├── credentials/<channel>/<acct>.json # per-channel secrets
├── mcp.json                          # (optional) MCP servers to import
├── memory.db, sessions.db, ...       # auto-created sqlite stores
├── approvals.json                    # auto-managed
└── cron/jobs.json                    # auto-managed
```

`SAMPYCLAW_HOME=/some/path` overrides the root.

---

## Start / Stop

### Foreground

```bash
SAMPYCLAW_GATEWAY_TOKEN=$(openssl rand -hex 32) \
  sampyclaw gateway start \
    --provider pi --model gemma4:latest \
    --port 7331
```

The startup performs **preflight validation** automatically. To skip
(e.g. emergency boot with a known-bad config), pass `--skip-preflight`.

### Bind policy: loopback by default

`sampyclaw gateway start` binds to `127.0.0.1` by default and **refuses
to bind to any non-loopback address** unless you explicitly opt in. The
intent: the agent runs as the local OS user and is reachable only by
that user on this machine. Anything wider should be a deliberate,
loud choice.

```bash
# Refused (no opt-in):
sampyclaw gateway start --host 0.0.0.0
# Error: refusing to bind gateway to wildcard (all interfaces) host '0.0.0.0'.
#        sampyClaw defaults to loopback so the agent runs only for the
#        local OS user on this machine. To bind beyond loopback (reverse
#        proxy, k8s Service, internal corp net), pass --allow-non-loopback
#        or set SAMPYCLAW_ALLOW_NON_LOOPBACK=1 …

# Allowed (explicit opt-in):
sampyclaw gateway start --host 0.0.0.0 --allow-non-loopback \
    --auth-token "$TOKEN" \
    --allowed-origins "https://dashboard.example.com"
# Logs a loud WARNING noting that the principal model has widened.
```

When binding to `0.0.0.0` for a reverse-proxy / k8s Service setup,
**always** combine the opt-in with `--auth-token` and
`--allowed-origins` — token-based auth still applies, but Origin
filtering protects the WS upgrade against CSRF from foreign browser
tabs.

#### WSL2 + Windows desktop app

Loopback enforcement still does the right thing when the agent runs
inside WSL2 and a Windows-side client (browser, Tauri desktop app)
connects to it. **You do NOT need `--allow-non-loopback` for the
WSL → Windows scenario.** The OS-level loopback bridge handles the
cross-namespace hop:

| WSL2 mode | WSL `127.0.0.1:7331` | Windows-side reach | LAN-side reach |
|---|---|---|---|
| **Mirrored** (Win11 22H2+, recommended — `~/.wslconfig: networkingMode=mirrored`) | loopback only | ✅ same loopback namespace as Windows | ❌ Windows firewall + loopback |
| **NAT + `localhostForwarding=true`** (default) | loopback only | ✅ `wslhost.exe` proxies Windows `localhost:7331` ↔ WSL `127.0.0.1:7331` | ❌ proxy listens on Windows loopback only |
| NAT + `localhostForwarding=false` | loopback only | ❌ Windows must use the WSL eth0 IP, which is non-loopback | ❌ |
| Bridged (Hyper-V manual) | WSL has a real LAN IP | requires binding to the WSL IP (non-loopback → opt-in) | ✅ |

So the recommended setup is:

```ini
# %USERPROFILE%\.wslconfig
[wsl2]
networkingMode=mirrored      # Win11 22H2+. Cleanest path — same loopback namespace.
# Or, on older Windows:
# localhostForwarding=true   # default, but make it explicit.
```

```bash
# Inside WSL — no opt-in needed, just the standard loopback default:
sampyclaw gateway start
```

```text
# On Windows — Tauri desktop app or browser:
http://localhost:7331/
```

The Windows desktop app's first-run wizard (`docs/DESKTOP_APP.md`)
defaults the gateway URL to `http://localhost:7331` for exactly this
reason. The principal model stays "this Windows user + their WSL
user only" — other LAN machines, other Windows users, other WSL
distros cannot reach the gateway.

Edge cases:

- **`localhostForwarding=false`**: the operator has explicitly
  disabled the Windows ↔ WSL loopback bridge. Either re-enable it
  or accept that you'll need to bind to the WSL eth0 IP (which is
  non-loopback → requires `--allow-non-loopback`). This is the
  expected behaviour, not a bug — the user opted out of the bridge.
- **WSL bridged mode**: WSL has a real LAN IP, so binding to it is
  by definition non-loopback. Use `--allow-non-loopback` and place
  a TLS-terminating proxy in front, same as a bare-metal LAN
  deployment.
- **WSL1**: shares the Windows kernel + network stack, so loopback
  is literally the same as Windows loopback. Behaves like mirrored
  mode for our purposes.

### systemd unit (recommended)

```ini
# /etc/systemd/system/sampyclaw.service
[Unit]
Description=sampyClaw gateway
After=network.target

[Service]
Type=simple
User=sampyclaw
EnvironmentFile=/etc/sampyclaw/env
ExecStart=/usr/local/bin/sampyclaw gateway start --host 127.0.0.1 --port 7331
Restart=on-failure
RestartSec=5
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
```

Then `/etc/sampyclaw/env`:

```
SAMPYCLAW_GATEWAY_TOKEN=...
SAMPYCLAW_HOME=/var/lib/sampyclaw
SAMPYCLAW_LOG_FORMAT=json
```

`systemctl reload sampyclaw` is **not** supported — config changes
require restart. Graceful shutdown is automatic on SIGTERM (drains
in-flight RPCs, closes channels, flushes WAL — see `gateway.shutdown` in
the metrics during the drain window).

### Docker / k8s

Bind to `0.0.0.0:7331` *with* `--allow-non-loopback` (or
`SAMPYCLAW_ALLOW_NON_LOOPBACK=1` in the env), and put a TLS-terminating
reverse proxy in front (nginx, Caddy, traefik). Probe endpoints:

- `livenessProbe`: `GET /healthz` → 200 = process alive
- `readinessProbe`: `GET /readyz` → 200 = ready, 503 = degraded/down

Set `terminationGracePeriodSeconds: 20` (gateway draws 10s default plus
margin for k8s itself).

---

## Observability

### `/metrics` — Prometheus

Plain text Prometheus exposition format. Always-on, unauthenticated.
Scrape interval ≥ 5s recommended.

Key metrics:

| Metric | Type | Labels | Use for |
|---|---|---|---|
| `sampyclaw_ws_connections_active` | gauge | — | dashboard / capacity |
| `sampyclaw_ws_rpc_total` | counter | method | request rate per RPC |
| `sampyclaw_ws_rpc_errors_total` | counter | method | error rate alerting |
| `sampyclaw_ws_rpc_duration_seconds` | histogram | method | p99 latency |
| `sampyclaw_channel_inbound_total` | counter | channel | per-channel inbound rate |
| `sampyclaw_channel_outbound_errors_total` | counter | channel | send failure alerting |
| `sampyclaw_agent_turn_duration_seconds` | histogram | agent_id | turn latency |
| `sampyclaw_tool_call_errors_total` | counter | tool | tool failure alerting |
| `sampyclaw_mcp_servers_connected` | gauge | — | MCP fleet health |
| `sampyclaw_cron_jobs_active` | gauge | — | scheduler depth |
| `sampyclaw_approvals_pending` | gauge | — | human-in-the-loop backlog |

Sample alerts:

```yaml
- alert: SampyClawHighErrorRate
  expr: |
    rate(sampyclaw_ws_rpc_errors_total[5m])
      / rate(sampyclaw_ws_rpc_total[5m]) > 0.05
  for: 10m

- alert: SampyClawTurnLatencyP99High
  expr: |
    histogram_quantile(0.99,
      rate(sampyclaw_agent_turn_duration_seconds_bucket[5m])
    ) > 30

- alert: SampyClawApprovalBacklog
  expr: sampyclaw_approvals_pending > 10
  for: 30m
```

### `/healthz` — liveness

Always returns 200 if the process responds. Use as the k8s liveness
probe — restart if not 200.

### `/readyz` — readiness

JSON body with per-probe breakdown. Returns 503 when any **critical**
probe is `down`. Default critical probes: `cron` (scheduler running).
Non-critical probes (channels, memory, metrics) demote overall to
`degraded` but keep the endpoint at 200.

```bash
curl -s http://127.0.0.1:7331/readyz | jq
```

### Logs

Two formats controlled by `SAMPYCLAW_LOG_FORMAT`:

- **`human`** (default) — readable text, suffixed with `[trace_id=…]`
  when a correlation context is active.
- **`json`** — one JSON object per line. Schema:
  ```json
  {"ts":"...","level":"INFO","logger":"sampyclaw.gateway.server",
   "message":"...","pid":123,"trace_id":"abc123","rpc":"chat.send"}
  ```

Every WS RPC carries a fresh `trace_id` (12-char hex) plus the `rpc`
method name, so following a single request through tool calls, agent
turns, channel sends is straightforward via grep / log query language.

In production, ship to your log aggregator (Loki, OpenSearch, Datadog…)
and grep on `trace_id` to follow a single RPC end-to-end.

---

## Backup / Restore

### Create

```bash
sampyclaw backup create /var/backups/sampyclaw/
```

Captures the entire `~/.sampyclaw/` tree:
- All sqlite DBs are snapshotted via SQLite's online `.backup` API, so
  the snapshot is consistent even with the gateway running. WAL/SHM
  siblings are skipped (re-derived from the snapshot DB on restore).
- All credentials, config, sessions, cron jobs, approvals, wiki pages
  are copied byte-for-byte.
- A `MANIFEST.json` with a SHA256 per file is added.

The output is a single `.tar.gz` named
`sampyclaw-backup-<YYYYMMDD-HHMMSS>.tar.gz`.

Retention: cron a weekly create + a monthly one. The archive is
self-contained and cheap to copy off-host.

### Verify

```bash
sampyclaw backup verify /var/backups/sampyclaw/sampyclaw-backup-20260425-031500.tar.gz
```

Replays SHA256 against every file in the manifest; non-zero exit means
the archive is corrupt — do not rely on it for restore.

### Restore

```bash
# Dry-run (recommended first):
sampyclaw backup restore --dry-run /var/backups/.../sampyclaw-backup-*.tar.gz

# Real restore — refuses to overwrite a non-empty home dir by default:
SAMPYCLAW_HOME=/var/lib/sampyclaw-restored \
  sampyclaw backup restore /var/backups/.../sampyclaw-backup-*.tar.gz

# Or merge into existing (preserves files NOT in the backup):
sampyclaw backup restore --overwrite /var/backups/.../sampyclaw-backup-*.tar.gz
```

After a restore, restart the gateway. Cron jobs are reloaded on
startup; pending approvals are surfaced in logs as "carried over".

---

## Upgrades

1. `sampyclaw backup create` (always, before any upgrade)
2. `pip install -U sampyclaw[dev]` (or your deployment mechanism)
3. `sampyclaw config validate` — schema may have evolved
4. `systemctl restart sampyclaw`
5. Watch `/readyz` until 200, watch metrics for error spikes.

If a regression appears, restore the backup and pin the previous
version.

---

## Common operational scenarios

### "Gateway is up but `/readyz` is 503"

`curl /readyz | jq` returns the failing probe. Most common causes:

- `cron` probe down → APScheduler has crashed; restart the service.
- `channels` probe degraded → no channels registered (config issue).
- `memory` probe down → memory store unreachable; check disk + WAL files.

### "WS clients can't connect"

- Verify `SAMPYCLAW_GATEWAY_TOKEN` matches what the client sends.
- Check `/healthz` is 200 (process alive).
- Look for `rejecting WS upgrade` warnings in logs.

### "Tool calls all failing with 'unavailable: server X is not connected'"

An MCP server failed to start. `pool.failures` in logs has the reason.
Common causes:
- Binary missing (`npx not found`)
- Token env var unset (`sampyclaw config validate` would catch this)
- Network policy blocking the server (e.g. NetPolicy denies the URL)

### "Approvals piling up"

Either operator is asleep, or the approver token is wrong (calls fail
silently with `UNAUTHORIZED`). Check
`sampyclaw_approvals_resolved_total{status="..."}` — non-zero `error`
status is the symptom. `ApprovalManager` persists pending requests so a
restart will not drop them.

### "Memory store keeps growing"

Check `pi/store_ops.MaintenanceConfig` is wired (it isn't by default
yet; opt-in). Manual prune:

```python
from sampyclaw.pi.store_ops import prune_by_age
prune_by_age(conn, days=30, keep_min=100)
```

Schedule via cron job for automatic enforcement.

---

## Capacity / soak testing

The repo ships a soak harness:

```bash
python scripts/soak.py \
  --duration 14400 \
  --rps 5 \
  --csv /var/log/sampyclaw/soak-$(date +%Y%m%d).csv \
  --max-rss-growth-kb 102400 \
  --max-fd-growth 30
```

Exit code 0 means: zero RPC errors, RSS growth under threshold, FD
growth under threshold. The CSV is suitable for plotting in any
spreadsheet or Grafana CSV data source.

Recommended cadence: run a 4h soak in CI before each release.

---

## Security operations

- Loopback is the default and is strictly enforced. To bind beyond
  it (LAN IP, `0.0.0.0`, `::`) you must pass `--allow-non-loopback`
  or set `SAMPYCLAW_ALLOW_NON_LOOPBACK=1` — the startup refuses
  otherwise. Always pair non-loopback exposure with a TLS-terminating
  reverse proxy AND an `--allowed-origins` list.
- Always set `SAMPYCLAW_GATEWAY_TOKEN` in production. The startup will
  warn loudly if it's missing.
- Rotate tokens by setting a new value and restarting; clients will need
  the new value.
- For approval-gated tool calls, set `SAMPYCLAW_APPROVER_TOKEN` to a
  separate value from the gateway token so that resolving an approval
  requires explicit possession of the approver credential.
- See `docs/SECURITY.md` for the full threat model and the layered
  defenses (skill scanner, sandbox, NetPolicy, DNS pinning, audit
  store).

---

## Quick reference

| Action | Command |
|---|---|
| Start | `sampyclaw gateway start` |
| Stop | `systemctl stop sampyclaw` (SIGTERM is graceful) |
| Validate config | `sampyclaw config validate` |
| Backup | `sampyclaw backup create <dir>` |
| Verify backup | `sampyclaw backup verify <archive>` |
| Restore | `sampyclaw backup restore <archive>` |
| Soak test | `python scripts/soak.py --duration N` |
| Print paths | `sampyclaw paths` |
| Print backup home | `sampyclaw backup home` |
| List backups | `sampyclaw backup list <dir>` |
| List skills | `sampyclaw skills list` |
| List sessions | `sampyclaw session list` |
| Wiki ops | `sampyclaw wiki list/show/lint/...` |

| Endpoint | Auth | Use |
|---|---|---|
| `/healthz` | open | k8s liveness, systemd watchdog |
| `/readyz` | open | k8s readiness |
| `/metrics` | open | Prometheus scrape |
| `/` `/dashboard` `/app.html` `/static/*` | open (assets) | Bundled web dashboard SPA (now with right-side **canvas panel** for CV-1 output). Loads anonymously; JS renders an in-app login gate when no token is found and uses it for the WS connect. |
| WS upgrade | **token** | Real auth boundary. Token via `Authorization: Bearer`, `?token=` on the WS URL, or the `sampyclaw_token` cookie. Canvas push events ride the same WS. |

### Optional toolsets

| Env var | Effect | Doc |
|---|---|---|
| `SAMPYCLAW_ENABLE_BROWSER=1` | Register the BR-1 browser tools (`browser_navigate`, `_snapshot`, `_screenshot`, `_click`, `_fill`) on every new agent. Requires `pip install 'sampyclaw[browser]' && playwright install chromium`. Combine with `SAMPYCLAW_NET_ALLOW_HOSTS=...` to widen the closed-by-default policy. | [`BROWSER.md`](./BROWSER.md) |
| `SAMPYCLAW_ENABLE_CANVAS=1` | Register the CV-1 canvas tools (`canvas_present`, `canvas_hide`) on every new agent. The dashboard panel + RPCs are always wired; this env-var only governs *agent-side* tool injection. | [`CANVAS.md`](./CANVAS.md) |
| `SAMPYCLAW_NET_ALLOW_HOSTS` | Comma-separated host allowlist for the shared `NetPolicy` (used by web tool, browser tool, MCP HTTP transports). | [`SECURITY.md`](./SECURITY.md) |
| `SAMPYCLAW_AUDIT_OUTBOUND=1` | Log every outbound HTTP from `aiohttp` *and* the browser route handler into `~/.sampyclaw/outbound-audit.db`. | [`SECURITY.md`](./SECURITY.md) |

### First-run token bootstrap

When `sampyclaw gateway start` boots without `--auth-token` and without
`SAMPYCLAW_GATEWAY_TOKEN`, the gateway auto-generates a 48-character hex
token, persists it to `~/.sampyclaw/gateway-token` (mode `0600`), and
prints a banner with the value plus a one-shot URL:

```
────────────────────────────────────────────────────────────
  sampyClaw gateway ready
────────────────────────────────────────────────────────────
  • a fresh gateway token was generated and saved to /home/me/.sampyclaw/gateway-token
  • token: e97856a899ee6c990bc2c59941d5dc9f995560ce444ce10e
  • open: http://127.0.0.1:7331/?token=e97856a899ee6c990bc2c59941d5dc9f995560ce444ce10e
────────────────────────────────────────────────────────────
```

Subsequent starts read the same token from the file and reuse it.
Resolution precedence (highest first): `--auth-token <value>` →
`SAMPYCLAW_GATEWAY_TOKEN` env → persisted file → freshly generated.

Manage the token with:

```bash
sampyclaw gateway token             # show current token + path
sampyclaw gateway token --rotate    # generate a new one (invalidates previous)
sampyclaw gateway token --no-show   # print only the path
```

For production: prefer setting `SAMPYCLAW_GATEWAY_TOKEN` from a real
secret manager and not relying on the persisted file. The file is
convenient for single-host installs and dev loops.

### Dashboard authentication flow

1. Operator opens `http://host:7331/` in a browser. The HTML, CSS, and
   JS load anonymously (matches openclaw's `control-ui` pattern).
2. The dashboard JS calls `WebSocket(ws://host:7331/?token=...)` using
   whatever token it can find in:
   - the `?token=` query string (one-shot URL login),
   - the `sampyclaw_token` cookie (set by the gateway after a
     successful query-token load, or by the in-app form),
   - `localStorage["sampyclaw_token"]` (also set by the in-app form).
3. If the WS upgrade is rejected (the gateway 401's anything missing
   the token), the JS shows a full-screen login gate with a token
   input. The user pastes the token, optionally checks "Remember on
   this device (12h cookie)", and clicks *Connect*.
4. The submit handler stores the token (cookie + localStorage when
   *Remember* is on) and retries the connect.
5. On the next reload the dashboard finds the token in the cookie /
   localStorage and connects without prompting.

For headless / scripted clients, `Authorization: Bearer <token>` is
still accepted on the WS upgrade.
