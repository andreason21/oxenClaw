# Windows desktop app + WSL agent

End-to-end setup: agent runs in WSL2, native desktop app runs on
Windows 11, talks to the gateway over loopback. Tauri delivers
Action Center toast notifications with action buttons, stores the
bearer token in the Windows Credential Manager (DPAPI-encrypted),
and refuses cross-origin WS upgrades that aren't from the desktop
app itself.

## Why this stack

| Concern | Browser bookmark | PWA | **Tauri (this doc)** |
|---|---|---|---|
| Token storage | `localStorage` + cookie (any browser extension can read) | same | **Windows Credential Manager (DPAPI, kernel-protected)** |
| Permission popup for notifications | required | required | not needed (pre-authorised at install) |
| Action buttons on notifications | ❌ | partial | **✅ Approve / Deny on the toast** |
| Tray icon, runs without browser | ❌ | ❌ | **✅** |
| Origin-locked WS upgrade | ❌ | ❌ | **✅** (gateway `--allowed-origins tauri://localhost`) |
| Auto-start on login + WSL launch | manual | partial | **✅** (built-in toggles) |

## Prerequisites

- Windows 11 22H2+ (mirrored networking is best, but `localhostForwarding=true` works too).
- WSL2 with a Linux distro (Ubuntu 24.04 tested). `wsl --install -d Ubuntu-24.04`.
- Python 3.11+ inside the distro: `sudo apt install python3 python3-venv`.
- Edge WebView2 runtime (preinstalled on Win11; the Tauri installer also bundles a bootstrapper).

## Step 1 — Install the agent in WSL

Inside WSL:

```bash
mkdir -p ~/sampyclaw && cd ~/sampyclaw
python3 -m venv venv && source venv/bin/activate
pip install -e /path/to/sampyclaw    # or `pip install sampyclaw` once published
```

Generate a token and start the gateway with the Tauri-friendly Origin
allowlist:

```bash
sampyclaw gateway token              # prints the bearer token
sampyclaw gateway start \
    --port 7331 \
    --allowed-origins "tauri://localhost,http://localhost:7331"
```

Confirm from inside Windows:

```powershell
curl http://localhost:7331/healthz   # → "ok"
```

If `localhost:7331` doesn't reach WSL, your `~/.wslconfig` may have
disabled forwarding. Either set `localhostForwarding=true` (legacy)
or `networkingMode=mirrored` (Win11 recommended). See [the WSL
networking docs](https://learn.microsoft.com/windows/wsl/networking).

For long-running deployments, register a `systemd` user unit inside
WSL (the `docs/OPERATIONS.md` file has a copy-paste template).

## Step 2 — Build the desktop app

The repo ships the Tauri scaffolding under `desktop/`. To build a
local debug binary on Windows:

```powershell
cd C:\path\to\sampyClaw\desktop
cargo tauri dev
```

For a release `.msi`:

```powershell
cargo tauri build
# desktop\src-tauri\target\release\bundle\msi\sampyclaw_<version>_x64_en-US.msi
```

CI builds the `.msi` automatically on every tag push (`v*`) — see
`.github/workflows/desktop-build.yml`. Download the artifact, sign it
with your org's authenticode cert:

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 \
              /a sampyclaw_*_x64_en-US.msi
```

## Step 3 — First run

Double-click the `.msi` to install. The app appears in Start menu and
the system tray (`🦞`). On first launch a setup screen asks for:

- **Gateway URL** — `http://localhost:7331` for the WSL-hosted setup
  above. For a remote gateway, use that host's URL; the connection
  goes over WS, so wrap it with HTTPS / mTLS at your reverse proxy.
- **Bearer token** — paste the value `sampyclaw gateway token` printed
  in Step 1. Stored in Credential Manager; **not** in any file the
  desktop app's WebView can read.
- (optional) **Auto-start on login** — Windows Task Scheduler entry.
- (optional) **WSL auto-launch** — runs `wsl ~ -e sampyclaw gateway
  start ...` if the gateway isn't already responding.

After Connect the dashboard SPA loads in the native window. Closing
the window minimises to the tray; right-click the tray icon → Quit
to exit fully.

## Notifications

The dashboard subscribes to gateway events and surfaces three of them
as native Action Center toasts:

| Event | Title | Action buttons | Behaviour |
|---|---|---|---|
| `approval_requested` | "Approval needed" | (planned: Approve / Deny) | Shown always — explicit human action required |
| `reply_complete` | "Reply from \<agent_id\>" | Click → focus dashboard | Suppressed when the dashboard window is focused |
| `cron_fired` | "Cron job fired" | Click → focus dashboard | Suppressed when focused |

The `Notify` module in `sampyclaw/static/app.js` routes through
`window.__TAURI__.core.invoke("show_notification", ...)` when the
Tauri runtime is detected, falling back to the browser
Notifications API otherwise. So the same SPA delivers toasts in
both browser and desktop modes — the desktop app just gets the
nicer-looking, no-permission-popup version.

## Security notes

- The gateway's `--allowed-origins` flag locks WS upgrades to specific
  Origin headers — without it, any browser tab on the same host
  can speak to the gateway given the token. Recommended values:
  `tauri://localhost` (this app) plus `http://localhost:7331` (browser
  fallback access). Add corp domains as needed.
- Token rotation: `sampyclaw gateway token --rotate` invalidates the
  prior token; the desktop app surfaces the next reconnect failure as
  a setup-screen prompt asking for the new value.
- The Tauri WebView is configured with
  `enableContextMenu: false` and a strict CSP. Use `cargo tauri dev`
  for debugging — the release build hides developer tools and the
  Windows console.
- The IPC capability list (`desktop/src-tauri/capabilities/default.json`)
  is deny-by-default. Adding new IPC commands also requires editing
  that file.
- The bundled WebView2 is updated by Edge's auto-updater, so the
  underlying browser engine gets security patches without a sampyClaw
  release.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| App opens, immediately shows setup screen on every launch | Token not stored — Credential Manager API may have failed. Check Windows Credential Manager → "ai.sampyclaw.desktop". |
| "origin not allowed" 403 on WS upgrade | Add `tauri://localhost` to `--allowed-origins`. |
| Dashboard loads but events don't push | WS connect succeeded but WAS rejected after handshake — check gateway logs for `Origin` warnings. |
| Notifications show but no sound | Windows Focus Assist is on. Adjust under Settings → Notifications. |
| WSL auto-launch fails | `wsl.exe` not in PATH (rare) or default distro disabled — toggle off WSL auto-launch and start the gateway manually with `wsl ~ -e sampyclaw gateway start`. |

## Future work

- Notification action buttons that fire `exec-approvals.resolve`
  directly from the toast (currently the toast just focuses the
  Approvals view).
- Built-in mTLS toggle for non-loopback gateway URLs.
- Code-signing pipeline integration (CI ↔ Hashicorp Vault PKI or
  Windows hardware token).
- macOS `.dmg` and Linux `.AppImage` from the same Tauri source —
  out-of-scope until there's demand.
