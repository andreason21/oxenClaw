// Hide the Windows console window in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod notify;
mod token;
mod wsl;

use serde::{Deserialize, Serialize};
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, RunEvent, WindowEvent,
};

#[derive(Debug, Serialize, Deserialize, Clone, Default)]
struct ConnectionInfo {
    pub gateway_url: String,
    pub token_set: bool,
}

// ─── IPC commands exposed to the renderer ─────────────────────────────

#[tauri::command]
fn save_token(token: String, gateway_url: String) -> Result<(), String> {
    token::save(&token::StoredToken { token, gateway_url })
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn forget_token() -> Result<(), String> {
    token::forget().map_err(|e| e.to_string())
}

/// Rewrite `localhost` to `127.0.0.1` in a gateway URL.
///
/// WebView2 follows Happy Eyeballs and prefers IPv6 (`[::1]`) when the
/// hostname is `localhost`; if the gateway only listens on IPv4 (the
/// default), navigation fails with ERR_CONNECTION_REFUSED while curl /
/// PowerShell (which fall back to IPv4) succeed — confusing failure
/// mode. Sanitising on read covers users who saved `http://localhost:7331`
/// before the desktop default flipped to `127.0.0.1`. Only the host
/// segment is rewritten; ports / paths / query strings are preserved.
fn force_ipv4_loopback(url: &str) -> String {
    url.replacen("://localhost:", "://127.0.0.1:", 1)
        .replacen("://localhost/", "://127.0.0.1/", 1)
}

/// Returns the configured URL with `?token=...` appended so the
/// renderer can navigate to it without ever seeing the raw token.
#[tauri::command]
fn connect_url() -> Result<String, String> {
    let stored = token::load().map_err(|e| e.to_string())?;
    let s = stored.ok_or("no token stored — run setup")?;
    let url = force_ipv4_loopback(&s.gateway_url);
    let sep = if url.contains('?') { '&' } else { '?' };
    Ok(format!(
        "{}{}token={}",
        url.trim_end_matches('/'),
        sep,
        s.token
    ))
}

#[tauri::command]
fn connection_info() -> Result<ConnectionInfo, String> {
    let stored = token::load().map_err(|e| e.to_string())?;
    Ok(ConnectionInfo {
        gateway_url: stored
            .as_ref()
            .map(|s| force_ipv4_loopback(&s.gateway_url))
            .unwrap_or_default(),
        token_set: stored.is_some(),
    })
}

#[tauri::command]
fn show_notification(app: AppHandle, payload: notify::NotifyPayload) -> Result<(), String> {
    notify::show(&app, payload)
}

#[tauri::command]
fn launch_wsl_gateway(opts: wsl::WslLaunchOptions) -> Result<(), String> {
    wsl::launch(&opts).map_err(|e| e.to_string())
}

#[tauri::command]
fn focus_main_window(app: AppHandle) {
    notify::focus_main(&app);
}

/// HTTP-probe a gateway URL to confirm it's actually listening from the
/// WebView2 process's network namespace.
///
/// Why a Rust IPC instead of `fetch("/healthz")` from the renderer? The
/// renderer's origin is `tauri://localhost`; `fetch()` to `http://127.0.0.1:7331/healthz`
/// is a cross-origin request, and Chromium-based WebView2 enforces
/// CORS + Private Network Access policies that the gateway doesn't
/// satisfy by default. PowerShell sees 200 because it's not a browser;
/// the renderer's fetch fails before the body even reaches JS. Doing
/// the request natively bypasses CORS entirely.
///
/// Why a real HTTP request instead of bare TCP connect? Bare TCP
/// open+close makes the gateway's websockets library log "did not
/// receive a valid HTTP request" at ERROR level on every probe —
/// noisy. A real HTTP request completes a normal transaction, and as
/// a bonus we learn the HTTP layer is alive (not just the port).
///
/// Why GET, not HEAD? The gateway's websockets-library-based HTTP
/// router doesn't handle HEAD on `/healthz` — it closes the
/// connection without responding (verified: PowerShell HEAD fails
/// with "underlying connection closed", GET returns 200). GET is
/// what production probes use anyway.
///
/// Why HTTP/1.1, not HTTP/1.0? Same reason — websockets-library only
/// dispatches HTTP/1.1 requests through `process_request`. A 1.0
/// request gets silently closed (verified: `nc` with HTTP/1.0
/// returned no bytes; with HTTP/1.1 returned 200 OK). The library
/// historically only spoke 1.1 because that's what the WebSocket
/// upgrade flow needs.
/// Returns a granular status string instead of a bool so the renderer
/// can surface *which* layer failed: DNS, TCP, HTTP write, response
/// read, or the response status code itself. Bool was masking real
/// network-stack issues.
#[tauri::command]
fn probe_gateway(url: String) -> Result<String, String> {
    use std::io::{Read, Write};
    use std::net::{TcpStream, ToSocketAddrs};
    use std::time::Duration;

    let parsed = url::Url::parse(&url).map_err(|e| format!("invalid url: {e}"))?;
    let host = parsed.host_str().ok_or("url has no host")?;
    let port = parsed
        .port_or_known_default()
        .ok_or("url has no port and scheme has no default")?;
    let target = format!("{host}:{port}");
    let addrs: Vec<_> = target
        .to_socket_addrs()
        .map_err(|e| format!("dns_resolve_failed for {target}: {e}"))?
        .collect();
    if addrs.is_empty() {
        return Ok(format!("dns_no_addrs for {target}"));
    }
    let timeout = Duration::from_millis(2500);
    let mut last_step = String::from("unknown");
    for addr in &addrs {
        last_step = format!("connect_attempt {addr}");
        let mut stream = match TcpStream::connect_timeout(addr, timeout) {
            Ok(s) => s,
            Err(e) => {
                last_step = format!("connect_failed {addr}: {} ({:?})", e, e.kind());
                continue;
            }
        };
        let _ = stream.set_read_timeout(Some(timeout));
        let _ = stream.set_write_timeout(Some(timeout));
        let req = format!(
            "GET /healthz HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\nUser-Agent: oxenclaw-desktop-probe\r\n\r\n"
        );
        if let Err(e) = stream.write_all(req.as_bytes()) {
            last_step = format!("write_failed {addr}: {e}");
            continue;
        }
        let mut buf = [0u8; 256];
        let n = match stream.read(&mut buf) {
            Ok(n) => n,
            Err(e) => {
                last_step = format!("read_failed {addr}: {e}");
                continue;
            }
        };
        if n == 0 {
            last_step = format!("read_empty {addr} — gateway closed connection without responding");
            continue;
        }
        let head = std::str::from_utf8(&buf[..n]).unwrap_or("(non-utf8)");
        let first_line = head.lines().next().unwrap_or("");
        if first_line.starts_with("HTTP/1.")
            && first_line.split_whitespace().nth(1).is_some_and(|c| c.starts_with('2'))
        {
            return Ok(format!("ok {addr} {first_line}"));
        }
        last_step = format!("non_2xx {addr}: {first_line}");
    }
    Ok(last_step)
}

// Auto-updater wiring (check_for_updates IPC + spawn_update_check + the
// tauri-plugin-updater registration) was removed in rc.13. It can't ship
// without a real Tauri signing keypair (the placeholder pubkey crashes
// app startup at plugin init). Restore from git history when signing is
// wired up; see desktop/src-tauri/Cargo.toml comment for the checklist.

#[cfg(test)]
mod tests {
    use super::force_ipv4_loopback;

    #[test]
    fn rewrites_localhost_host() {
        assert_eq!(
            force_ipv4_loopback("http://localhost:7331"),
            "http://127.0.0.1:7331"
        );
        assert_eq!(
            force_ipv4_loopback("http://localhost:7331/path?x=1"),
            "http://127.0.0.1:7331/path?x=1"
        );
        assert_eq!(
            force_ipv4_loopback("https://localhost/admin"),
            "https://127.0.0.1/admin"
        );
    }

    #[test]
    fn probe_rejects_bad_url() {
        use super::probe_gateway;
        assert!(probe_gateway("not a url".to_string()).is_err());
        assert!(probe_gateway("http://".to_string()).is_err());
    }

    #[test]
    fn probe_returns_connect_failed_on_closed_port() {
        use super::probe_gateway;
        // Port 1 on loopback is never listened on; the connect attempt
        // should fail and the status string should start with
        // `connect_failed` so the renderer can surface that.
        let r = probe_gateway("http://127.0.0.1:1".to_string()).expect("probe should not Err");
        assert!(
            r.starts_with("connect_failed") || r.starts_with("connect_attempt"),
            "got {r:?}"
        );
    }

    #[test]
    fn leaves_other_hosts_unchanged() {
        assert_eq!(
            force_ipv4_loopback("http://127.0.0.1:7331"),
            "http://127.0.0.1:7331"
        );
        assert_eq!(
            force_ipv4_loopback("http://gateway.corp:7331"),
            "http://gateway.corp:7331"
        );
        // Substring `localhost` inside path/query must not be rewritten.
        assert_eq!(
            force_ipv4_loopback("http://example.com/?host=localhost"),
            "http://example.com/?host=localhost"
        );
    }
}

// ─── Tray ────────────────────────────────────────────────────────────

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "Show oxenClaw", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &quit])?;
    // Tauri 2 ignores the declarative `trayIcon` section in tauri.conf.json
    // — without an explicit `.icon()` call the tray draws a generic /
    // squashed glyph because `default_window_icon` isn't auto-applied to
    // the tray. Reuse the bundled app icon (16/32-px tracks live inside
    // icons/icon.ico).
    let mut tray = TrayIconBuilder::with_id("main")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .tooltip("oxenClaw");
    if let Some(icon) = app.default_window_icon().cloned() {
        tray = tray.icon(icon);
    }
    tray
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => notify::focus_main(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            // Single-click on the tray icon brings the window forward.
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                notify::focus_main(tray.app_handle());
            }
        })
        .build(app)?;
    Ok(())
}

// ─── Diagnostic panic hook ───────────────────────────────────────────
//
// Release builds set `windows_subsystem = "windows"` (no console), so a
// Tauri Builder panic exits silently — exactly the symptom users see
// when *anything* in plugin init or `generate_context!()` fails. Install
// a panic hook *before* Tauri's Builder runs so any startup panic is
// persisted to disk under `%LOCALAPPDATA%\oxenClaw\panic.log` (or
// `~/.local/share/oxenclaw/panic.log` on Linux). The file is appended
// to so multiple crashes are preserved.

fn install_panic_hook() {
    let original = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let log_path = panic_log_path();
        let body = format!(
            "==== oxenClaw panic at {ts} ====\n{info}\nbacktrace:\n{bt}\n\n",
            ts = chrono_now_string(),
            info = info,
            bt = std::backtrace::Backtrace::capture(),
        );
        if let Some(path) = log_path.as_ref() {
            let _ = std::fs::create_dir_all(path.parent().unwrap_or(std::path::Path::new(".")));
            let _ = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(path)
                .and_then(|mut f| std::io::Write::write_all(&mut f, body.as_bytes()));
        }
        // Still defer to the default handler so dev (debug) builds with a
        // console see the message in stderr.
        original(info);
    }));
}

fn panic_log_path() -> Option<std::path::PathBuf> {
    #[cfg(target_os = "windows")]
    {
        if let Ok(local) = std::env::var("LOCALAPPDATA") {
            return Some(std::path::PathBuf::from(local).join("oxenClaw").join("panic.log"));
        }
    }
    #[cfg(target_os = "linux")]
    {
        if let Ok(home) = std::env::var("HOME") {
            return Some(
                std::path::PathBuf::from(home)
                    .join(".local")
                    .join("share")
                    .join("oxenclaw")
                    .join("panic.log"),
            );
        }
    }
    None
}

fn chrono_now_string() -> String {
    // Avoid pulling in chrono — use std::time + a manual UTC format.
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!("epoch_secs={secs}")
}

// ─── App entry ───────────────────────────────────────────────────────

fn main() {
    install_panic_hook();
    env_logger::init();

    let builder = tauri::Builder::default()
        // Register single-instance FIRST so the second .exe launch hands
        // its CWD/args off to the running process before any other plugin
        // (or window) work happens. Without this, repeated double-clicks
        // on the .exe spawn ghost processes that all show stale state.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_store::Builder::default().build())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_os::init())
        .plugin(tauri_plugin_process::init())
        .invoke_handler(tauri::generate_handler![
            save_token,
            forget_token,
            connect_url,
            connection_info,
            show_notification,
            launch_wsl_gateway,
            focus_main_window,
            probe_gateway,
        ])
        .setup(|app| {
            build_tray(app.handle())?;
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the main window minimises to the tray instead of
            // exiting — the tray Quit item is the explicit shutdown.
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        });

    let app = match builder.build(tauri::generate_context!()) {
        Ok(a) => a,
        Err(e) => {
            // Persist the build error so users have something concrete
            // to share — mirrors the panic hook surface.
            if let Some(path) = panic_log_path() {
                let _ = std::fs::create_dir_all(
                    path.parent().unwrap_or(std::path::Path::new(".")),
                );
                let _ = std::fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(&path)
                    .and_then(|mut f| {
                        std::io::Write::write_all(
                            &mut f,
                            format!(
                                "==== oxenClaw Builder.build failed at {ts} ====\n{err}\n\n",
                                ts = chrono_now_string(),
                                err = e,
                            )
                            .as_bytes(),
                        )
                    });
            }
            std::process::exit(1);
        }
    };

    app.run(|_app, event| {
        if let RunEvent::ExitRequested { .. } = event {
            // Allow exit when explicitly requested from the tray.
        }
    });
}
