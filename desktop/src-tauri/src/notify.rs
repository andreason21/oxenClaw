//! Native Action Center toasts.
//!
//! The renderer (web) calls `cmd_notify(...)` over IPC with a structured
//! payload describing the alert. We render it via `tauri-plugin-
//! notification`. On Windows that becomes a real WinRT toast; on
//! macOS/Linux the plugin uses each platform's native API.

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};
use tauri_plugin_notification::NotificationExt;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct NotifyPayload {
    pub kind: String,        // "reply" | "approval" | "cron" | "channel" | "info"
    pub title: String,
    pub body: String,
    pub correlation_id: Option<String>,
    #[serde(default)]
    pub actions: Vec<String>,
}

pub fn show(app: &AppHandle, payload: NotifyPayload) -> Result<(), String> {
    app.notification()
        .builder()
        .title(&payload.title)
        .body(&payload.body)
        .show()
        .map_err(|e| format!("notification failed: {e}"))?;
    Ok(())
}

pub fn focus_main(app: &AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
    }
}
