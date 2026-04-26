// Hide the Windows console window in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod notify;
mod token;
mod wsl;

use serde::{Deserialize, Serialize};
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, RunEvent, WindowEvent,
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

/// Returns the configured URL with `?token=...` appended so the
/// renderer can navigate to it without ever seeing the raw token.
#[tauri::command]
fn connect_url() -> Result<String, String> {
    let stored = token::load().map_err(|e| e.to_string())?;
    let s = stored.ok_or("no token stored — run setup")?;
    let sep = if s.gateway_url.contains('?') { '&' } else { '?' };
    Ok(format!(
        "{}{}token={}",
        s.gateway_url.trim_end_matches('/'),
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
            .map(|s| s.gateway_url.clone())
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

// Auto-updater wiring (check_for_updates IPC + spawn_update_check + the
// tauri-plugin-updater registration) was removed in rc.13. It can't ship
// without a real Tauri signing keypair (the placeholder pubkey crashes
// app startup at plugin init). Restore from git history when signing is
// wired up; see desktop/src-tauri/Cargo.toml comment for the checklist.

// ─── Tray ────────────────────────────────────────────────────────────

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "Show oxenClaw", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &quit])?;
    TrayIconBuilder::with_id("main")
        .menu(&menu)
        .show_menu_on_left_click(false)
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
