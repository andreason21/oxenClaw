# Slack outbound channel (alert/notification only)

`sampyclaw/extensions/slack/` ships an Enterprise-Grid-friendly,
**outbound-only** Slack channel. It posts messages via Slack Web API
`chat.postMessage` and never listens for inbound. The gateway's
monitor supervisor recognises `outbound_only = True` on the channel
plugin and skips spawning a polling task for it.

## What it does

- `chat.postMessage` on Slack Web API, with bot-token auth.
- Multi-account: one workspace = one `account_id` under `channels.slack.accounts`.
- Enterprise Grid: workspace bot tokens (`xoxb-`) and org-wide tokens
  (`xoxe.xoxb-`) both work — Slack treats them the same on the call.
- Threaded replies via `target.thread_id` (Slack's `thread_ts`).
- 429 retry honouring Slack's `Retry-After`; full-jitter backoff on 5xx
  and connection drops (max 3 retries).
- Outbound goes through `security/net/guarded_session()` — SSRF, DNS
  pinning, scheme + port checks apply automatically.

## What it deliberately doesn't do

| Feature | Why |
|---|---|
| Events API webhook ingestion | Needs public ingress + HMAC verification + a FastAPI route. Out of scope for "alerts only". |
| Socket Mode (WS inbound) | Requires `connections.open` + a long-lived listener. Not needed for outbound. |
| File uploads (`files.upload`) | Multi-step flow with size limits. Media items in `SendParams` get a one-line "(N attachment(s))" summary instead. |
| Interactive blocks (buttons/menus) | Slack's interactive payloads are bidirectional — they belong with inbound. |
| Slack Connect cross-org channels | Works automatically once the bot is installed in both workspaces; nothing extra to do. |

If you need any of the above, write an external `slack-inbound-sampyclaw`
plugin — the SDK supports it (see `sampyclaw/extensions/telegram/` for
the bidirectional pattern).

## Setup

### 1. Get a bot token

Create a Slack app under your Enterprise Grid org:

- **Workspace bot**: install the app to the target workspace, copy the
  `xoxb-...` token from "OAuth & Permissions". Required scopes for
  outbound-only: `chat:write`. Add `chat:write.public` if you want to
  post to channels the bot isn't a member of.
- **Org-wide bot** (Grid only): enable org-wide deployment, copy the
  `xoxe.xoxb-...` token.

### 2. Store the credential

```bash
mkdir -p ~/.sampyclaw/credentials/slack
cat > ~/.sampyclaw/credentials/slack/alerts.json <<'EOF'
{ "token": "xoxb-..." }
EOF
chmod 600 ~/.sampyclaw/credentials/slack/alerts.json
```

The single-bot shortcut also works: set `SLACK_BOT_TOKEN` in the env and
name the account `main`. Multi-account always needs the credentials
file.

### 3. Declare the account in `config.yaml`

```yaml
channels:
  slack:
    accounts:
      - account_id: alerts
        # Optional: per-account corp proxy. Default is https://slack.com/api.
        base_url: https://slack-proxy.corp.example/api
```

For Enterprise Grid with multiple workspaces, declare one account per
workspace — each gets its own `xoxb-...` credential file.

### 4. Make sure the egress can reach Slack

`security/net/policy.py:NetPolicy` is **closed-by-default** on hostname
allowlists when one is set. If your deployment uses
`SAMPYCLAW_NET_ALLOWED_HOSTNAMES`, add `slack.com` (or your corp proxy
hostname) to it:

```bash
export SAMPYCLAW_NET_ALLOWED_HOSTNAMES="slack.com,*.slack.com,slack-proxy.corp.example"
```

`HTTPS_PROXY` env vars are honoured automatically by aiohttp — no extra
sampyClaw config is needed for HTTP CONNECT proxies.

## Sending alerts

### From an agent turn

The bundled `message` tool already routes through the channel router:

```
"send 'deploy started for v1.2.3' to slack #alerts"
```

The agent calls `message(channel="slack", account_id="alerts",
chat_id="C0123ABCD", text=...)` and the rest is automatic.

### From cron

```bash
sampyclaw cron add \
    --schedule "0 9 * * 1-5" \
    --agent default \
    --channel slack --account_id alerts --chat_id C0123ABCD \
    --prompt "Summarise overnight production errors from the last 12h"
```

The cron tool registers the job; at 09:00 every weekday the agent runs
the prompt and the reply is delivered to `#alerts`.

### From the gateway dashboard

Open the dashboard, switch to the **Chat** tab, set the target to
`slack:alerts:C0123ABCD`, and Send. The reply is posted as the agent
in the configured channel — usefulness limited (no inbound), but handy
for one-off pings while you're already in the dashboard.

### Direct RPC

```json
{
  "jsonrpc": "2.0", "id": 1, "method": "chat.send",
  "params": {
    "channel": "slack", "account_id": "alerts",
    "chat_id": "C0123ABCD",
    "text": "deploy gate failed on staging"
  }
}
```

## Looking up `chat_id`

Slack channel names are pretty (`#alerts`) but sampyClaw expects the
internal channel ID (`C0123ABCD`) so a renamed channel doesn't silently
break the alert flow. Find it in Slack:

- Right-click the channel → "View channel details" → bottom of the panel.
- Or via Web API: `conversations.list` (needs `channels:read` scope —
  one-time lookup, not part of the outbound-only profile).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `slack: invalid_auth` | Token expired, revoked, or wrong workspace. Re-issue from "OAuth & Permissions". |
| `slack: not_in_channel` | Bot user isn't a member of `chat_id`. `/invite @bot-name` in the channel, or grant `chat:write.public` and post to public channels without joining. |
| `slack: channel_not_found` | Wrong channel ID, or workspace mismatch (token belongs to workspace A, channel ID is from workspace B). |
| `slack: ratelimited` | Slack tier-1 (`chat.postMessage` ≈ 1/sec sustained). The client honours `Retry-After`; if you're hitting it constantly, batch your alerts. |
| `web_fetch error: hostname 'slack.com' not allowed by policy` | Add `slack.com` (or your proxy host) to `SAMPYCLAW_NET_ALLOWED_HOSTNAMES`. |
| Connection refused / DNS fails on internal network | Set `base_url: https://slack-proxy.corp.example/api` and add the proxy host to the allowlist. Or set `HTTPS_PROXY` so aiohttp routes through your CONNECT proxy. |

## Future inbound (sketch — not implemented)

Adding inbound would mean:

1. A FastAPI ingress route at `/slack/events` that reads the request
   body, verifies the `X-Slack-Signature` HMAC header
   (`security/net/webhook_guards.py:verify_hmac_signature(prefix="v0=")`
   already does this), and pushes to a queue.
2. A `SlackInboundChannel` plugin whose `monitor()` drains that queue.
3. Slack Connect requires the bot to be in both orgs but adds no new
   schema; messages just arrive with a different `team_id` in the raw
   payload.

That's a 2–3 day chunk; ship it as `slack-inbound-sampyclaw` so
operators can pin or omit it.
