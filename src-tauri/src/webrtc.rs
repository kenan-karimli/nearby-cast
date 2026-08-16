use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SdpOffer {
    pub sdp_type: String,
    pub sdp: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IceCandidate {
    pub candidate: String,
    pub sdp_mid: String,
    pub sdp_mline_index: u16,
}

pub struct WebRtcTransport {
    pub is_connected: bool,
}

impl Default for WebRtcTransport {
    fn default() -> Self {
        Self::new()
    }
}

impl WebRtcTransport {
    pub fn new() -> Self {
        Self {
            is_connected: false,
        }
    }

    pub fn create_offer(&mut self) -> Result<SdpOffer, String> {
        Err(
            "WebRTC signalling is not implemented; refusing to create a fabricated SDP offer"
                .into(),
        )
    }

    pub fn process_answer(&mut self, _answer: &SdpOffer) -> Result<(), String> {
        Err("WebRTC signalling is not implemented; refusing to report a connection".into())
    }
}
