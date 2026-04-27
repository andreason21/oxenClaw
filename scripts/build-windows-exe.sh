#!/usr/bin/env bash
# Local cross-build helper: produce just the desktop client .exe from
# WSL2 (or any Linux box) for quick on-Windows smoke testing — skips
# the WiX/NSIS bundlers, which Tauri can't drive from Linux. Output:
#   desktop/src-tauri/target/x86_64-pc-windows-msvc/release/oxenclaw-desktop.exe
#
# Reach it from Windows at:
#   \\wsl$\Ubuntu\home\<user>\<repo>\desktop\src-tauri\target\x86_64-pc-windows-msvc\release\oxenclaw-desktop.exe
#
# Prereqs (one-time):
#   rustup target add x86_64-pc-windows-msvc
#   cargo install cargo-xwin
#   cargo install tauri-cli --version "^2.0"

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.cargo/bin:$PATH"

cd "$REPO_ROOT/desktop/src-tauri"
# `custom-protocol` is critical: without it Tauri 2 falls back to the
# `devUrl` (http://localhost:1420) instead of the embedded `frontendDist`,
# so the WebView navigates to a dev server that doesn't exist and the
# user only ever sees ERR_CONNECTION_REFUSED — never our index.html.
# `cargo tauri build` adds this flag automatically; raw `cargo xwin
# build` does not.
cargo xwin build --release --target x86_64-pc-windows-msvc \
    --no-default-features --features custom-protocol "$@"

exe="$REPO_ROOT/desktop/src-tauri/target/x86_64-pc-windows-msvc/release/oxenclaw-desktop.exe"
if [ ! -f "$exe" ]; then
    echo "ERROR: build succeeded but $exe is missing" >&2
    exit 1
fi
size_mb=$(du -h "$exe" | awk '{print $1}')
distro="${WSL_DISTRO_NAME:-Ubuntu}"
win_path="\\\\wsl\$\\${distro}${exe//\//\\}"

# Copy to a local Windows path under %LOCALAPPDATA% — running from
# `\\wsl$\...` (a UNC network share) puts the app in a security
# context where Win32 Credential Manager API calls silently fail
# (set_password returns ok but writes nothing). A local-disk copy
# avoids that gotcha entirely.
local_dir="$(powershell.exe -NoProfile -Command 'Write-Output $env:LOCALAPPDATA' 2>/dev/null | tr -d '\r')\\oxenclaw-dev"
if [ -n "${local_dir}" ]; then
    win_dest="${local_dir}\\oxenclaw-desktop.exe"
    # Convert the Windows-style destination path back to a WSL one for cp
    wsl_dest="$(wslpath "${local_dir}" 2>/dev/null)/oxenclaw-desktop.exe"
    if [ -n "${wsl_dest}" ]; then
        mkdir -p "$(dirname "${wsl_dest}")"
        cp -f "$exe" "${wsl_dest}"
        echo
        echo "✓ built: $exe ($size_mb)"
        echo "  WSL path:    $exe"
        echo "  UNC path:    $win_path  (do NOT run from here — Credential Manager fails)"
        echo "  Windows run: ${win_dest}  (run THIS one — local disk, full DPAPI access)"
        exit 0
    fi
fi

echo
echo "✓ built: $exe ($size_mb)"
echo "  Windows path: $win_path"
