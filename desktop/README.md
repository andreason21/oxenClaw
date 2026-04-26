# sampyClaw Desktop (Tauri)

Native Windows 11 desktop app that connects to a sampyClaw gateway —
typically running inside WSL2. Wraps the bundled dashboard SPA in a
WebView2 native window with the security and UX gains a browser tab
can't deliver:

- **Bearer token in Windows Credential Manager (DPAPI-encrypted)**, not
  browser localStorage. Other processes / browser extensions can't
  read it.
- **Native Action Center toasts** with **action buttons** ("Approve",
  "Deny", "View") — no browser permission popup, the app is
  pre-authorised at install.
- **System tray icon** — close the window, the app keeps running and
  delivers notifications.
- **Origin-locked WS upgrade** — the gateway can be configured with
  `--allowed-origins tauri://localhost` so cross-origin browser-based
  attacks against `ws://localhost:7331` are refused at handshake time.
- **Auto-start on Windows login** (configurable; off by default).
- **Optional WSL auto-launch** — the app spawns
  `wsl ~ -e sampyclaw gateway start` on first boot when the gateway
  isn't already reachable.

Without the desktop app the dashboard works fine in a browser at
`http://localhost:7331/?token=...`; this just hardens it.

## Layout

```
desktop/
├── README.md                 # this file
├── src-tauri/
│   ├── Cargo.toml            # Rust crate; depends on tauri + plugins
│   ├── tauri.conf.json       # Tauri build + bundle config (.msi)
│   ├── build.rs              # Tauri build script
│   ├── capabilities/
│   │   └── default.json      # IPC allowlist (deny-by-default)
│   ├── icons/                # Windows ICO + PNG assets
│   └── src/
│       ├── main.rs           # tray icon, lifecycle, plugin wiring
│       ├── token.rs          # Credential Manager (DPAPI) read/write
│       ├── notify.rs         # native toast with action buttons
│       └── wsl.rs            # WSL auto-launch helper
└── web/                      # Tauri's "distDir" — points at the gateway
    └── index.html            # 1-line redirect to gateway URL or login
```

The dashboard SPA itself ships from the gateway (`sampyclaw/static/`).
The Tauri app does not bundle a copy — it loads the live SPA over the
WS-authenticated HTTP route the user configures (default
`http://localhost:7331`).

## Build

Requirements (one-time on the build host, not on user machines):

```bash
# Rust toolchain
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Tauri CLI
cargo install tauri-cli --version "^2.0"
```

Build a debug binary:

```bash
cd desktop
cargo tauri dev
```

Build a release `.msi` for Windows (cross-build supported from Linux too):

```bash
cd desktop
cargo tauri build
# output: desktop/src-tauri/target/release/bundle/msi/sampyclaw-desktop_*_x64_en-US.msi
```

## Code-signing the `.msi`

For corporate distribution, sign the bundle:

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 \
              /a sampyclaw-desktop_*_x64_en-US.msi
```

CI: `.github/workflows/desktop-build.yml` builds the unsigned `.msi`
on every tag push (`v*`); signing happens manually with the org cert
(can be wired into the workflow if a `WINDOWS_CERT_PFX` secret is
provided).

## Runtime configuration

On first launch the app shows a setup screen asking for:

- **Gateway URL** (default `http://localhost:7331`)
- **Bearer token** (paste once; stored in Credential Manager)
- **WSL auto-launch** (off by default; turn on if the WSL distro and
  `sampyclaw gateway start` should be spawned by the desktop app)

These land in `%APPDATA%\sampyclaw-desktop\config.json` for non-secret
values; the token only ever hits Credential Manager.

## Security posture

- `tauri.conf.json` runs with `withGlobalTauri=false` and the IPC
  allowlist denies everything not explicitly granted.
- WebView2 has `enableContextMenu: false` to suppress "View source"
  / "Inspect element" by default. (Operators with debug needs run
  `cargo tauri dev` instead.)
- The WebView only loads URLs matching the configured gateway URL +
  `tauri://localhost`. Any other navigation attempt is blocked by
  Tauri's URL allowlist.
- Token rotation: rotate inside the gateway (`sampyclaw gateway token
  --rotate`); the desktop app picks up the new value on its next
  reconnect, prompting the user to paste it.

See `docs/DESKTOP_APP.md` for end-user install + first-run
instructions across Windows and Ubuntu, and `docs/SECURITY.md` for
the full threat model.
