//! Google Cast session adapter.
//!
//! Production Google Cast media is owned by `cast_launcher.py` (low-latency HLS
//! + pychromecast / lab load).
//!
//! This module exposes readiness helpers used by diagnostics and capability reporting.
pub struct GoogleCastSession;

impl Default for GoogleCastSession {
    fn default() -> Self {
        Self::new()
    }
}

impl GoogleCastSession {
    pub fn new() -> Self {
        Self
    }

    pub fn is_sender_available() -> bool {
        which("python3").is_some()
    }

    pub fn start_stream(&mut self, _target_ip: &str, _output_name: &str) -> Result<u32, String> {
        Err(
            "Google Cast sessions are managed by the Tauri projection service via cast_launcher.py"
                .into(),
        )
    }

    pub fn stop(&mut self) {}
}

fn which(binary: &str) -> Option<std::path::PathBuf> {
    std::env::var_os("PATH").and_then(|paths| {
        for path in std::env::split_paths(&paths) {
            let candidate = path.join(binary);
            if candidate.is_file() {
                return Some(candidate);
            }
        }
        None
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reports_sender_availability_from_python() {
        assert!(GoogleCastSession::is_sender_available());
    }
}
