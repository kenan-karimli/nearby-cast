//! Deterministic protocol choice. Advertisement is intentionally not enough:
//! a protocol is eligible only when the local sender path is available.
//!
//! Scoring prefers lower practical end-to-end latency among eligible transports:
//! Miracast (RTP) < NearbyCast (MPEG-TS) < AirPlay lab (MPEG-TS) < Google Cast (fMP4/HLS).
use crate::discovery::DetectedProtocol;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RouteInput {
    pub advertised: Vec<DetectedProtocol>,
    pub google_cast_ready: bool,
    pub miracast_ready: bool,
    pub nearby_cast_ready: bool,
    pub airplay_ready: bool,
}

/// Approximate practical latency class used only for ranking eligible protocols.
/// Lower is better. Values are relative ranks, not measured milliseconds.
fn latency_rank(protocol: DetectedProtocol) -> u8 {
    match protocol {
        DetectedProtocol::Miracast => 1,
        DetectedProtocol::NearbyCast => 2,
        DetectedProtocol::AirPlay => 3,
        DetectedProtocol::GoogleCast => 4,
        DetectedProtocol::Unknown => 9,
    }
}

pub fn select_protocol(input: &RouteInput) -> Option<DetectedProtocol> {
    let eligible = |protocol: DetectedProtocol, available: bool| {
        available && input.advertised.contains(&protocol)
    };

    let mut candidates = Vec::new();
    if eligible(DetectedProtocol::Miracast, input.miracast_ready) {
        candidates.push(DetectedProtocol::Miracast);
    }
    if eligible(DetectedProtocol::NearbyCast, input.nearby_cast_ready) {
        candidates.push(DetectedProtocol::NearbyCast);
    }
    if eligible(DetectedProtocol::AirPlay, input.airplay_ready) {
        candidates.push(DetectedProtocol::AirPlay);
    }
    if eligible(DetectedProtocol::GoogleCast, input.google_cast_ready) {
        candidates.push(DetectedProtocol::GoogleCast);
    }
    candidates.sort_by_key(|protocol| latency_rank(protocol.clone()));
    candidates.into_iter().next()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn never_selects_an_advertisement_without_a_local_sender() {
        let input = RouteInput {
            advertised: vec![DetectedProtocol::AirPlay, DetectedProtocol::Miracast],
            google_cast_ready: true,
            miracast_ready: false,
            nearby_cast_ready: false,
            airplay_ready: false,
        };
        assert_eq!(select_protocol(&input), None);
    }

    #[test]
    fn prefers_low_latency_verified_wfd() {
        let input = RouteInput {
            advertised: vec![DetectedProtocol::GoogleCast, DetectedProtocol::Miracast],
            google_cast_ready: true,
            miracast_ready: true,
            nearby_cast_ready: false,
            airplay_ready: false,
        };
        assert_eq!(select_protocol(&input), Some(DetectedProtocol::Miracast));
    }

    #[test]
    fn selects_airplay_when_lab_sender_ready() {
        let input = RouteInput {
            advertised: vec![DetectedProtocol::AirPlay, DetectedProtocol::GoogleCast],
            google_cast_ready: true,
            miracast_ready: false,
            nearby_cast_ready: false,
            airplay_ready: true,
        };
        assert_eq!(select_protocol(&input), Some(DetectedProtocol::AirPlay));
    }

    #[test]
    fn ranks_nearby_above_cast_hls() {
        let input = RouteInput {
            advertised: vec![DetectedProtocol::NearbyCast, DetectedProtocol::GoogleCast],
            google_cast_ready: true,
            miracast_ready: false,
            nearby_cast_ready: true,
            airplay_ready: false,
        };
        assert_eq!(select_protocol(&input), Some(DetectedProtocol::NearbyCast));
    }
}
