//! Optional helper to spawn `oxenclaw gateway start` inside WSL when
//! the configured gateway URL is unreachable.
//!
//! Off by default — the user opts in via the setup screen. The helper
//! does NOT keep the WSL process alive across desktop app shutdowns
//! (that's a `systemd` unit's job inside WSL). It only kicks the
//! gateway awake on first launch when nothing is listening yet.

use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct WslLaunchOptions {
    /// WSL distro name (e.g. "Ubuntu-24.04"). When None, default distro.
    pub distro: Option<String>,
    /// Command to run inside the distro. Default:
    /// `oxenclaw gateway start --port 7331 --allowed-origins tauri://localhost`.
    pub command: Option<String>,
}

#[cfg(target_os = "windows")]
pub fn launch(opts: &WslLaunchOptions) -> std::io::Result<()> {
    use std::process::Command;
    let cmd = opts.command.as_deref().unwrap_or(
        "oxenclaw gateway start --port 7331 --allowed-origins tauri://localhost",
    );
    let mut wsl = Command::new("wsl.exe");
    if let Some(d) = &opts.distro {
        wsl.arg("-d").arg(d);
    }
    wsl.arg("--").arg("sh").arg("-lc").arg(cmd);
    // Detach so the desktop app isn't tied to WSL's lifetime.
    wsl.spawn().map(|_| ())
}

#[cfg(not(target_os = "windows"))]
pub fn launch(_opts: &WslLaunchOptions) -> std::io::Result<()> {
    // WSL launch is Windows-only. On macOS/Linux dev builds this is a no-op.
    Ok(())
}
