//! Bearer-token storage backed by the OS keychain.
//!
//! On Windows this is the Credential Manager (DPAPI-encrypted, per-user,
//! readable only by processes running as the same user — kernel-level
//! protection). On macOS it's Keychain; on Linux it's libsecret /
//! gnome-keyring (the Linux build is for `cargo tauri dev` on a dev
//! laptop, not a shipping target).
//!
//! The token is NEVER touched by the WebView's JS bridge — the renderer
//! asks Rust for the URL+token via the `connection_info` IPC command,
//! which constructs the connect URL inline. This avoids exposing the
//! raw secret to JS at all.

use keyring::Entry;
use serde::{Deserialize, Serialize};

const SERVICE: &str = "ai.sampyclaw.desktop";
const ACCOUNT: &str = "gateway-token";

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct StoredToken {
    pub token: String,
    pub gateway_url: String,
}

#[derive(thiserror::Error, Debug)]
pub enum TokenError {
    #[error("keyring: {0}")]
    Keyring(#[from] keyring::Error),
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
}

fn entry() -> Result<Entry, keyring::Error> {
    Entry::new(SERVICE, ACCOUNT)
}

pub fn save(stored: &StoredToken) -> Result<(), TokenError> {
    let blob = serde_json::to_string(stored)?;
    entry()?.set_password(&blob)?;
    Ok(())
}

pub fn load() -> Result<Option<StoredToken>, TokenError> {
    match entry()?.get_password() {
        Ok(blob) => Ok(Some(serde_json::from_str(&blob)?)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(e.into()),
    }
}

pub fn forget() -> Result<(), TokenError> {
    match entry()?.delete_credential() {
        Ok(()) => Ok(()),
        Err(keyring::Error::NoEntry) => Ok(()),
        Err(e) => Err(e.into()),
    }
}
