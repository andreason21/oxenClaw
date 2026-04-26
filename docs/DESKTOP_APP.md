# Desktop app (Windows + Ubuntu) + agent

End-to-end setup: the agent gateway runs anywhere (WSL2 on Windows,
a Linux server, the same Linux box running the desktop app, or a
remote host), and a native desktop client connects to it. The same
Tauri source ships:

- **Windows 11** — `.msi` (system-wide, MSI-managed via group policy /
  SCCM / winget) and NSIS `.exe` (per-user, no admin). Token in
  Credential Manager (DPAPI-encrypted). Action Center toasts with
  action buttons. Optional WSL auto-launch.
- **Ubuntu 22.04 + 24.04** — `.deb` (one per distro because of
  glibc/webkit ABI differences) and a single `.AppImage` that runs
  on both. Token in libsecret (gnome-keyring / KWallet).
  freedesktop notifications. Tray icon via libayatana-appindicator.

All builds are produced by `.github/workflows/release.yml` on `v*`
tag push and attached to the GitHub Release. The desktop app
auto-updates against `latest.json` on launch (Ed25519-signed) — on
Linux only the AppImage is auto-updated; `.deb` users `apt upgrade`
or download a new bundle.

## Why this stack

| Concern | Browser bookmark | PWA | **Tauri (this doc)** |
|---|---|---|---|
| Token storage | `localStorage` + cookie (any browser extension can read) | same | **OS keychain** — Credential Manager (Win) / libsecret (Linux) — kernel-protected |
| Permission popup for notifications | required | required | not needed (pre-authorised at install) |
| Action buttons on notifications | ❌ | partial | **✅ Approve / Deny on the toast** |
| Tray icon, runs without browser | ❌ | ❌ | **✅** |
| Origin-locked WS upgrade | ❌ | ❌ | **✅** (gateway `--allowed-origins tauri://localhost`) |
| Auto-start on login + WSL launch | manual | partial | **✅** (built-in toggles) |
| Auto-update | manual reload | manual reload | **✅** Ed25519-signed deltas via `latest.json` |

## Prerequisites

### Windows
- Windows 11 22H2+ (mirrored networking is best, but `localhostForwarding=true` works too).
- WSL2 with a Linux distro for the agent (Ubuntu 24.04 tested). `wsl --install -d Ubuntu-24.04`.
- Python 3.11+ inside the distro: `sudo apt install python3 python3-venv`.
- Edge WebView2 runtime (preinstalled on Win11; the Tauri installer also bundles a bootstrapper).
- (build-host only) **WiX 3.11 toolset** for `.msi` packaging:
  `choco install wixtoolset --version=3.11.2`. Tauri 2.x does **not**
  auto-install WiX — without it `cargo tauri build` silently drops
  the `.msi` and only ships the NSIS `.exe`.

### Ubuntu (22.04 / 24.04)
- libwebkit2gtk-4.1, libayatana-appindicator3, libgtk-3 — the `.deb`
  declares these as dependencies so apt pulls them automatically.
  The `.AppImage` bundles its own libwebkit2gtk so it runs on hosts
  without the dev libraries.
- Python 3.11+ for the agent: `sudo apt install python3 python3-venv`
  (24.04 ships 3.12; 22.04 ships 3.10 — install `python3.11` from the
  deadsnakes PPA on 22.04, or use the AppImage path which just needs
  the desktop app, not the agent).
- A keyring service for credential storage. GNOME and KDE provide
  one out of the box; on a headless / Sway / minimal install do
  `sudo apt install gnome-keyring`.

## Step 1 — Install the agent in WSL

Inside WSL:

```bash
mkdir -p ~/oxenclaw && cd ~/oxenclaw
python3 -m venv venv && source venv/bin/activate
pip install -e /path/to/oxenclaw    # or `pip install oxenclaw` once published
```

Generate a token and start the gateway with the Tauri-friendly Origin
allowlist:

```bash
oxenclaw gateway token              # prints the bearer token
oxenclaw gateway start \
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

> **Bind policy reminder**: the gateway binds to `127.0.0.1` only by
> default. That's still correct for the WSL → Windows desktop app
> path — both mirrored and NAT-with-`localhostForwarding` modes
> bridge Windows `localhost:7331` to WSL's loopback at the OS level,
> so you do **not** need `--allow-non-loopback` for this setup. See
> [`OPERATIONS.md` § WSL2 + Windows desktop app](OPERATIONS.md#wsl2--windows-desktop-app)
> for the full per-mode matrix and edge cases.

For long-running deployments, register a `systemd` user unit inside
WSL (the `docs/OPERATIONS.md` file has a copy-paste template).

## Step 2 — Get the desktop app

You don't usually need to build it yourself. CI publishes pre-built
bundles to the GitHub Release on every `v*` tag — see
`.github/workflows/release.yml`.

### Pre-built (recommended)

Pick the matching artifact from the latest release page:

| Platform | File | Install |
|---|---|---|
| Windows 11 | `oxenclaw_X.Y.Z_x64_en-US.msi` | double-click; or `winget install oxenClaw.oxenClaw` after the first winget-pkgs PR merges |
| Windows 11 | `oxenClaw_X.Y.Z_x64-setup.exe` (NSIS) | double-click; per-user install, no admin |
| Ubuntu 22.04 | `oxenclaw_X.Y.Z_amd64_ubuntu22.04.deb` | `sudo apt install ./oxenclaw_*.deb` |
| Ubuntu 24.04 | `oxenclaw_X.Y.Z_amd64_ubuntu24.04.deb` | `sudo apt install ./oxenclaw_*.deb` |
| Any Linux | `oxenclaw_X.Y.Z_amd64_*.AppImage` | `chmod +x *.AppImage && ./oxenclaw_*.AppImage` |

The `.AppImage` runs on both 22.04 and 24.04 (and other glibc 2.31+
distros) because it bundles its own webkit2gtk. The two `.deb`s are
distro-specific because they declare ABI-tied dependencies; pick the
one matching your `lsb_release -rs`.

### Local build (developers only)

The repo ships the Tauri scaffolding under `desktop/`. Debug:

```bash
cd desktop
cargo tauri dev
```

Release on Windows. WiX 3.11 must be on PATH (see prereqs above);
without it the .msi step is silently skipped:

```powershell
choco install -y wixtoolset --version=3.11.2
$env:Path += ";${env:ProgramFiles(x86)}\WiX Toolset v3.11\bin"
cargo tauri build --bundles msi nsis
# desktop\src-tauri\target\release\bundle\msi\oxenclaw_<version>_x64_en-US.msi
# desktop\src-tauri\target\release\bundle\nsis\oxenClaw_<version>_x64-setup.exe
```

Release on Ubuntu (after installing `libwebkit2gtk-4.1-dev`,
`libayatana-appindicator3-dev`, `librsvg2-dev`, `libsoup-3.0-dev`,
`libssl-dev`, `patchelf`, `file`):

```bash
cargo tauri build --bundles deb appimage
# desktop/src-tauri/target/release/bundle/deb/oxenclaw_<version>_amd64.deb
# desktop/src-tauri/target/release/bundle/appimage/oxenclaw_<version>_amd64.AppImage
```

CI runs both paths on every PR via `desktop-build.yml`; release
versions go through `release.yml`. To codesign the Windows bundle
locally:

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 \
              /a oxenclaw_*_x64_en-US.msi
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 \
              /a oxenClaw_*_x64-setup.exe
```

## Step 3 — First run

Open the app (Start menu on Windows, `oxenclaw-desktop` from
`apt`-installed `.deb`, or run the `.AppImage` directly). The app
appears in the system tray (`🐂🐂`). On first launch a setup screen
asks for:

- **Gateway URL** — `http://localhost:7331` for a local agent. For a
  remote gateway, use that host's URL; wrap with HTTPS/mTLS at your
  reverse proxy if you cross any non-loopback network.
- **Bearer token** — paste the value `oxenclaw gateway token` printed
  in Step 1. Stored in the OS keychain (Credential Manager on
  Windows, libsecret on Linux); **not** in any file the WebView can
  read.
- (optional) **Auto-start on login** — Task Scheduler on Windows,
  `~/.config/autostart/` on Linux.
- (optional, Windows only) **WSL auto-launch** — runs
  `wsl ~ -e oxenclaw gateway start ...` if the gateway isn't
  already responding.

After Connect the dashboard SPA loads in the native window. Closing
the window minimises to the tray; right-click the tray icon → Quit
to exit fully.

## Notifications

The dashboard subscribes to gateway events and surfaces three of them
as native OS toasts (Windows Action Center, GNOME/KDE notification
daemon, etc.):

| Event | Title | Action buttons | Behaviour |
|---|---|---|---|
| `approval_requested` | "Approval needed" | (planned: Approve / Deny) | Shown always — explicit human action required |
| `reply_complete` | "Reply from \<agent_id\>" | Click → focus dashboard | Suppressed when the dashboard window is focused |
| `cron_fired` | "Cron job fired" | Click → focus dashboard | Suppressed when focused |

The `Notify` module in `oxenclaw/static/app.js` routes through
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
- Token rotation: `oxenclaw gateway token --rotate` invalidates the
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
  underlying browser engine gets security patches without a oxenClaw
  release.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| App opens, immediately shows setup screen on every launch (Windows) | Token not stored — Credential Manager API may have failed. Check Credential Manager → "ai.oxenclaw.desktop". |
| App opens, immediately shows setup screen on every launch (Linux) | No keyring service running — install `gnome-keyring` (`sudo apt install gnome-keyring`) or unlock KWallet, log out and back in. |
| `.deb` install fails: "depends on libwebkit2gtk-4.1-0; however …" | Wrong distro file — install `*_ubuntu22.04.deb` on 22.04, `*_ubuntu24.04.deb` on 24.04. Or use the AppImage which has no apt deps. |
| AppImage doesn't launch on first try | Missing FUSE: `sudo apt install fuse libfuse2`. Or extract: `./oxenclaw_*.AppImage --appimage-extract` and run the extracted binary. |
| Tray icon missing on GNOME | Install AppIndicator support: `sudo apt install gnome-shell-extension-appindicator` and enable it in Extensions. |
| "origin not allowed" 403 on WS upgrade | Add `tauri://localhost` to `--allowed-origins`. |
| Dashboard loads but events don't push | WS connect succeeded but was rejected after handshake — check gateway logs for `Origin` warnings. |
| Notifications show but no sound (Windows) | Focus Assist is on. Adjust under Settings → Notifications. |
| WSL auto-launch fails | `wsl.exe` not in PATH (rare) or default distro disabled — toggle off WSL auto-launch and start the gateway manually with `wsl ~ -e oxenclaw gateway start`. |
| Auto-updater silent on Linux despite new release | Auto-update only ships via the AppImage path. `.deb` users `apt install ./<new>.deb`. |

## Future work

- Notification action buttons that fire `exec-approvals.resolve`
  directly from the toast (currently the toast just focuses the
  Approvals view).
- Built-in mTLS toggle for non-loopback gateway URLs.
- Code-signing pipeline integration (CI ↔ Hashicorp Vault PKI or
  Windows hardware token).
- Self-hosted apt repo / Launchpad PPA so Ubuntu users can `apt
  install oxenclaw` instead of downloading the `.deb`.
- macOS `.dmg` from the same Tauri source — out-of-scope until
  there's demand.
