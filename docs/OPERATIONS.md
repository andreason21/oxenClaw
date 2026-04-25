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
    --provider pi --model qwen2.5:7b-instruct \
    --host 0.0.0.0 --port 7331
```

The startup performs **preflight validation** automatically. To skip
(e.g. emergency boot with a known-bad config), pass `--skip-preflight`.

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

Bind to `0.0.0.0:7331` and put a TLS-terminating reverse proxy in front
(nginx, Caddy, traefik). Probe endpoints:

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

- Bind to `127.0.0.1` and rely on a TLS-terminating reverse proxy unless
  you have a reason to expose `0.0.0.0`.
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

| Endpoint | Use |
|---|---|
| `/healthz` | k8s liveness, systemd watchdog |
| `/readyz` | k8s readiness |
| `/metrics` | Prometheus scrape |
| `/` | Bundled web dashboard |
| WS upgrade | All RPC traffic (Authorization: Bearer required) |
