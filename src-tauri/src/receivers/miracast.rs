//! Miracast is delegated to FluxCast rather than the stale MiracleCast source
//! code.  This boundary is deliberately small: FluxCast owns NetworkManager
//! P2P, WFD RTSP negotiation and RTP media, while NearbyCast owns device UI,
//! session ownership and lifecycle.
use std::path::{Path, PathBuf};
use std::process::Command;

pub const FLUXCAST_ENV: &str = "NEARBY_CAST_FLUXCAST";

fn candidate_is_usable(path: &Path) -> bool {
    path.is_file()
}

/// Resolve the FluxCast WFD helper without shell interpolation.
///
/// Order:
/// 1. `NEARBY_CAST_FLUXCAST` (absolute file or bare command name)
/// 2. `PATH` lookup for `fluxcast`
/// 3. Development checkout: `.venv-fluxcast/bin/fluxcast` next to the app root
pub fn fluxcast_path() -> Option<PathBuf> {
    if let Ok(value) = std::env::var(FLUXCAST_ENV) {
        let path = PathBuf::from(value);
        if candidate_is_usable(&path) || path.components().count() == 1 {
            return Some(path);
        }
    }
    if let Some(path) = which_fluxcast() {
        return Some(path);
    }
    development_fluxcast()
}

fn which_fluxcast() -> Option<PathBuf> {
    Command::new("which")
        .arg("fluxcast")
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .map(|value| PathBuf::from(value.trim()))
        .filter(|path| !path.as_os_str().is_empty() && candidate_is_usable(path))
}

fn development_fluxcast() -> Option<PathBuf> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let root = manifest_dir.parent()?;
    let candidate = root.join(".venv-fluxcast/bin/fluxcast");
    candidate_is_usable(&candidate).then_some(candidate)
}

pub fn fluxcast_available() -> bool {
    fluxcast_path().is_some()
}

/// Direct WFD RTSP lab sender used when the peer is an IPv4 sink instead of a
/// Wi-Fi Direct MAC. Physical P2P association is intentionally out of scope.
pub fn wfd_lab_sender_path() -> Option<PathBuf> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let candidate = manifest_dir
        .parent()?
        .join("tools/virtual-receivers/miracast/lab_sender.py");
    candidate.is_file().then_some(candidate)
}

pub fn wfd_lab_sender_available() -> bool {
    wfd_lab_sender_path().is_some()
}

/// A WFD peer selector is either the P2P MAC address or a friendly name. It
/// is passed as one argument to the helper, never through a shell.
pub fn valid_peer_selector(value: &str) -> bool {
    let value = value.trim();
    if value.is_empty() || value.len() > 128 || value.chars().any(char::is_control) {
        return false;
    }

    if looks_like_mac(value) {
        return is_mac_address(value);
    }

    true
}

pub fn is_mac_address(value: &str) -> bool {
    let value = value.trim();
    value.len() == 17
        && value.bytes().enumerate().all(|(index, byte)| {
            if index % 3 == 2 {
                byte == b':'
            } else {
                byte.is_ascii_hexdigit()
            }
        })
}

fn looks_like_mac(value: &str) -> bool {
    value.len() == 17
        && value.as_bytes().iter().enumerate().all(
            |(index, &byte)| {
                if index % 3 == 2 {
                    byte == b':'
                } else {
                    true
                }
            },
        )
}

pub struct MiracastHandler;

impl MiracastHandler {
    pub fn is_supported() -> bool {
        fluxcast_available()
    }

    pub fn get_status_message(device_name: &str) -> String {
        if Self::is_supported() {
            format!(
                "Miracast receiver '{}' can be connected through the installed FluxCast WFD sender.",
                device_name
            )
        } else {
            format!(
                "Miracast receiver '{}' detected. Install the FluxCast WFD sender and ensure NetworkManager Wi-Fi Direct is available.",
                device_name
            )
        }
    }
}

#[cfg(test)]
mod tests {
    use super::valid_peer_selector;

    #[test]
    fn accepts_safe_wfd_peer_selectors() {
        assert!(valid_peer_selector("aa:bb:cc:dd:ee:ff"));
        assert!(valid_peer_selector("Living room display"));
        assert!(!valid_peer_selector(""));
        assert!(!valid_peer_selector("aa:bb:cc:dd:ee:zz"));
        assert!(!valid_peer_selector("tv\n--evil"));
    }
}
