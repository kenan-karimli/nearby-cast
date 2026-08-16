//! AirPlay sender path for Linux.
//!
//! Lab / third-party receivers that advertise `lab=1` (or `_airplay-lab`) use
//! the production AirPlay lab sender: PIN pair-setup → RTSP SETUP/RECORD →
//! authenticated MPEG-TS.
//!
//! Modern Apple devices that require FairPlay are discovered but not marked
//! castable until physical verification exists. Pairing attempts against them
//! fail honestly rather than faking success.
use std::path::PathBuf;

pub fn airplay_lab_sender_path() -> Option<PathBuf> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let candidate = manifest_dir
        .parent()?
        .join("tools/virtual-receivers/airplay/lab_sender.py");
    candidate.is_file().then_some(candidate)
}

pub fn airplay_lab_sender_available() -> bool {
    airplay_lab_sender_path().is_some()
}

pub struct AirPlayHandler;

impl AirPlayHandler {
    pub fn is_supported() -> bool {
        airplay_lab_sender_available()
    }

    pub fn get_status_message(device_name: &str) -> String {
        if Self::is_supported() {
            format!(
                "AirPlay lab mirroring is available for '{}'. Physical FairPlay Apple TV auth remains NOT VERIFIED.",
                device_name
            )
        } else {
            format!(
                "AirPlay receiver '{}' detected, but the AirPlay lab sender is missing from this install.",
                device_name
            )
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lab_sender_resolves_in_checkout() {
        assert!(airplay_lab_sender_available());
        assert!(AirPlayHandler::is_supported());
    }
}
