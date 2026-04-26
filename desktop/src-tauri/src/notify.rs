//! Native Action Center toasts with action buttons.
//!
//! The renderer (web) calls `cmd_notify(...)` over IPC with a structured
//! payload describing the alert. We render it via `tauri-plugin-
//! notification`. On Windows that becomes a real WinRT toast; on
//! macOS/Linux the plugin uses each platform's native API.
//!
//! Action buttons (e.g. "Approve" / "Deny" for an exec-approval
//! request) are emitted back to the renderer as `notify_action`
//! events; the renderer decides what RPC to fire.

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_notification::NotificationExt;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct NotifyPayload {
    pub kind: String,        // "reply" | "approval" | "cron" | "channel" | "info"
    pub title: String,
    pub body: String,
    pub correlation_id: Option<String>,
    /// Optional button labels — currently informational; click action
    /// is "focus the window and emit `notify_clicked`".
    #[serde(default)]
    pub actions: Vec<String>,
}

#[derive(Debug, Serialize, Clone)]
pub struct NotifyAction {
    pub correlation_id: Option<String>,
    pub action: String,
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

/// Bring the main window to the foreground when the user clicks a
/// notification. Used as the default click target if no specific
/// action is wired.
pub fn focus_main(app: &AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
    }
}

/// Emit a structured action event back to the renderer so the SPA can
/// fire the relevant RPC (`exec-approvals.resolve` etc).
pub fn emit_action(app: &AppHandle, action: NotifyAction) {
    let _ = app.emit("notify_action", action);
}
