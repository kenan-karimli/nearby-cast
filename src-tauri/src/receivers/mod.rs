pub mod airplay;
pub mod google_cast;
pub mod miracast;
pub mod nearby_cast;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ReceiverProtocol {
    NearbyCast,
    GoogleCast,
    AirPlay,
    Miracast,
    Unknown,
}

impl std::fmt::Display for ReceiverProtocol {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ReceiverProtocol::NearbyCast => write!(f, "Nearby Cast"),
            ReceiverProtocol::GoogleCast => write!(f, "Google Cast"),
            ReceiverProtocol::AirPlay => write!(f, "AirPlay"),
            ReceiverProtocol::Miracast => write!(f, "Miracast"),
            ReceiverProtocol::Unknown => write!(f, "Unknown"),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReceiverCapabilities {
    pub protocol: ReceiverProtocol,
    pub screen_cast: bool,
    pub media_cast: bool,
    pub audio_cast: bool,
    pub secure_pairing: bool,
    pub requires_pin: bool,
    pub support_notes: String,
}

impl ReceiverCapabilities {
    pub fn for_google_cast(device_name: &str) -> Self {
        Self {
            protocol: ReceiverProtocol::GoogleCast,
            screen_cast: true,
            media_cast: true,
            audio_cast: true,
            secure_pairing: false,
            requires_pin: false,
            support_notes: format!(
                "Supported via fragmented MP4 HTTP stream (HLS fallback available) on Google Cast receiver ({})",
                device_name
            ),
        }
    }

    pub fn for_nearby_cast(device_name: &str) -> Self {
        Self {
            protocol: ReceiverProtocol::NearbyCast,
            screen_cast: true,
            media_cast: false,
            audio_cast: true,
            secure_pairing: true,
            requires_pin: true,
            support_notes: format!(
                "NearbyCast authenticated streaming to {} requires pairing before the first trusted session",
                device_name
            ),
        }
    }

    pub fn for_airplay(device_name: &str) -> Self {
        let lab = airplay::airplay_lab_sender_available();
        Self {
            protocol: ReceiverProtocol::AirPlay,
            screen_cast: lab,
            media_cast: false,
            audio_cast: lab,
            secure_pairing: true,
            requires_pin: true,
            support_notes: airplay::AirPlayHandler::get_status_message(device_name),
        }
    }

    pub fn for_miracast(device_name: &str) -> Self {
        let supported = miracast::MiracastHandler::is_supported();
        Self {
            protocol: ReceiverProtocol::Miracast,
            screen_cast: supported,
            media_cast: false,
            audio_cast: supported,
            secure_pairing: false,
            requires_pin: false,
            support_notes: miracast::MiracastHandler::get_status_message(device_name),
        }
    }

    pub fn unknown(device_name: &str) -> Self {
        Self {
            protocol: ReceiverProtocol::Unknown,
            screen_cast: false,
            media_cast: false,
            audio_cast: false,
            secure_pairing: false,
            requires_pin: false,
            support_notes: format!(
                "Generic network device ({}) - protocol not identified",
                device_name
            ),
        }
    }
}
