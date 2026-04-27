//! Bearer-token storage.
//!
//! Originally backed by the OS keychain (`keyring` crate → Windows
//! Credential Manager / macOS Keychain / libsecret) for DPAPI-grade
//! per-user encryption. In practice the cross-build via `cargo-xwin`
//! produces binaries where `keyring`'s `set_password` returns Ok but
//! the entry never actually lands in Credential Manager — `get_password`
//! immediately returns `NoEntry`. Symptom: "save_token returned ok but
//! token_set is still false".
//!
//! Fallback: a JSON file under the platform's local app-data dir
//! (Windows `%LOCALAPPDATA%\oxenclaw-desktop\token.json`, Linux
//! `$XDG_DATA_HOME/oxenclaw-desktop/token.json`). NTFS ACLs on
//! `%LOCALAPPDATA%` already restrict reads to the same user, so the
//! security drop vs Credential Manager's DPAPI is small for a dev
//! tool. Production-signed builds via `cargo tauri build` should
//! reintroduce keyring once the cross-build lossiness is fixed.
//!
//! The token is NEVER touched by the WebView's JS bridge — the renderer
//! asks Rust for the URL+token via `connect_url` / `connection_info`,
//! never reading the secret directly.

use std::fs;
use std::io;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct StoredToken {
    pub token: String,
    pub gateway_url: String,
}

#[derive(thiserror::Error, Debug)]
pub enum TokenError {
    #[error("io: {0}")]
    Io(#[from] io::Error),
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
    #[error("could not resolve local app-data directory")]
    NoAppData,
}

fn token_path() -> Result<PathBuf, TokenError> {
    #[cfg(target_os = "windows")]
    let base = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .ok_or(TokenError::NoAppData)?;
    #[cfg(target_os = "linux")]
    let base = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local").join("share"))
        })
        .ok_or(TokenError::NoAppData)?;
    #[cfg(target_os = "macos")]
    let base = std::env::var_os("HOME")
        .map(|h| PathBuf::from(h).join("Library").join("Application Support"))
        .ok_or(TokenError::NoAppData)?;
    Ok(base.join("oxenclaw-desktop").join("token.json"))
}

pub fn save(stored: &StoredToken) -> Result<(), TokenError> {
    let path = token_path()?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let blob = serde_json::to_string(stored)?;
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, blob.as_bytes())?;
    fs::rename(&tmp, &path)?;
    Ok(())
}

pub fn load() -> Result<Option<StoredToken>, TokenError> {
    let path = token_path()?;
    match fs::read_to_string(&path) {
        Ok(blob) => Ok(Some(serde_json::from_str(&blob)?)),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.into()),
    }
}

pub fn forget() -> Result<(), TokenError> {
    let path = token_path()?;
    match fs::remove_file(&path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e.into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // Token storage hits a global env-var-resolved path; serialise the
    // env mutation across tests so they don't trample each other when
    // run in parallel.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn point_app_data_at(tmp: &std::path::Path) {
        #[cfg(target_os = "windows")]
        std::env::set_var("LOCALAPPDATA", tmp);
        #[cfg(target_os = "linux")]
        std::env::set_var("XDG_DATA_HOME", tmp);
        #[cfg(target_os = "macos")]
        std::env::set_var("HOME", tmp);
    }

    #[test]
    fn save_then_load_round_trip() {
        let _g = ENV_LOCK.lock().unwrap();
        let tmp = tempfile::tempdir().unwrap();
        point_app_data_at(tmp.path());
        // Linux non-XDG fallback uses HOME/.local/share — set HOME too
        // so the path is fully under tmp on every platform.
        std::env::set_var("HOME", tmp.path());

        let _ = forget();
        let stored = StoredToken {
            token: "abc-123".to_string(),
            gateway_url: "http://127.0.0.1:7331".to_string(),
        };
        save(&stored).expect("save should succeed");
        let loaded = load().expect("load should succeed").expect("expected Some");
        assert_eq!(loaded.token, "abc-123");
        assert_eq!(loaded.gateway_url, "http://127.0.0.1:7331");

        forget().expect("forget should succeed");
        let after = load().expect("load after forget should succeed");
        assert!(after.is_none(), "expected None, got {:?}", after);
    }

    #[test]
    fn load_returns_none_when_file_missing() {
        let _g = ENV_LOCK.lock().unwrap();
        let tmp = tempfile::tempdir().unwrap();
        point_app_data_at(tmp.path());
        std::env::set_var("HOME", tmp.path());
        // Nothing saved yet — load should be None, not Err.
        let _ = forget();
        assert!(load().expect("load ok").is_none());
    }
}
