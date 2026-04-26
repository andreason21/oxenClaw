// Hide the Windows console window in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod notify;
mod token;
mod wsl;

use serde::{Deserialize, Serialize};
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, RunEvent, WindowEvent,
};
use tauri_plugin_updater::UpdaterExt;

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

/// IPC: ask the renderer-side UI to trigger an update check now.
/// Spawns the async checker and emits `updater_status` events with
/// `{ status: "no-update" | "available" | "downloading" | "installed" | "error", ... }`
/// so the SPA can render progress without polling.
#[tauri::command]
async fn check_for_updates(app: AppHandle) -> Result<(), String> {
    spawn_update_check(app, true);
    Ok(())
}

#[derive(Debug, Serialize, Clone)]
struct UpdaterStatus {
    status: &'static str,
    version: Option<String>,
    notes: Option<String>,
    error: Option<String>,
    progress: Option<u64>,
    total: Option<u64>,
}

fn spawn_update_check(app: AppHandle, user_initiated: bool) {
    tauri::async_runtime::spawn(async move {
        let updater = match app.updater() {
            Ok(u) => u,
            Err(e) => {
                let _ = app.emit("updater_status", UpdaterStatus {
                    status: "error", version: None, notes: None,
                    error: Some(format!("updater unavailable: {e}")),
                    progress: None, total: None,
                });
                return;
            }
        };
        match updater.check().await {
            Ok(Some(update)) => {
                let v = update.version.clone();
                let notes = update.body.clone();
                let _ = app.emit("updater_status", UpdaterStatus {
                    status: "available",
                    version: Some(v.clone()),
                    notes: notes.clone(),
                    error: None, progress: None, total: None,
                });
                // Auto-download for both startup and user-initiated checks;
                // the SPA gets a final `installed` event and prompts for restart.
                let mut downloaded: u64 = 0;
                let app2 = app.clone();
                let result = update.download_and_install(
                    move |chunk_len, content_length| {
                        downloaded += chunk_len as u64;
                        let _ = app2.emit("updater_status", UpdaterStatus {
                            status: "downloading",
                            version: Some(v.clone()),
                            notes: None, error: None,
                            progress: Some(downloaded),
                            total: content_length,
                        });
                    },
                    || {},
                ).await;
                match result {
                    Ok(()) => {
                        let _ = app.emit("updater_status", UpdaterStatus {
                            status: "installed",
                            version: update.version.into(),
                            notes, error: None, progress: None, total: None,
                        });
                    }
                    Err(e) => {
                        let _ = app.emit("updater_status", UpdaterStatus {
                            status: "error", version: None, notes: None,
                            error: Some(format!("install failed: {e}")),
                            progress: None, total: None,
                        });
                    }
                }
            }
            Ok(None) => {
                if user_initiated {
                    let _ = app.emit("updater_status", UpdaterStatus {
                        status: "no-update", version: None, notes: None,
                        error: None, progress: None, total: None,
                    });
                }
            }
            Err(e) => {
                let _ = app.emit("updater_status", UpdaterStatus {
                    status: "error", version: None, notes: None,
                    error: Some(format!("check failed: {e}")),
                    progress: None, total: None,
                });
            }
        }
    });
}

// ─── Tray ────────────────────────────────────────────────────────────

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "Show sampyClaw", true, None::<&str>)?;
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

// ─── App entry ───────────────────────────────────────────────────────

fn main() {
    env_logger::init();

    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_store::Builder::default().build())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_os::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .invoke_handler(tauri::generate_handler![
            save_token,
            forget_token,
            connect_url,
            connection_info,
            show_notification,
            launch_wsl_gateway,
            focus_main_window,
            check_for_updates,
        ])
        .setup(|app| {
            build_tray(app.handle())?;
            // Background update check on launch — silent unless an
            // update is actually found.
            spawn_update_check(app.handle().clone(), false);
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
        })
        .build(tauri::generate_context!())
        .expect("failed to build tauri app")
        .run(|_app, event| {
            if let RunEvent::ExitRequested { .. } = event {
                // Allow exit when explicitly requested from the tray.
            }
        });
}
