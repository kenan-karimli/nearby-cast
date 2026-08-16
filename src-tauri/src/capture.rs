use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScreenSourceInfo {
    pub id: String,
    pub name: String,
    pub source_type: String,
    pub resolution: String,
}

pub trait ScreenCaptureEngine {
    fn list_sources(&self) -> Result<Vec<ScreenSourceInfo>, String>;
    fn start_capture(&self, source_id: &str) -> Result<u32, String>;
}

/// PipeWire / xdg-desktop-portal capture probe.
///
/// Enumeration uses `pw-cli` / portal readiness checks. Actual frame capture in
/// production casting still goes through the session-owned wf-recorder /
/// lab-media encoders so the cast lifecycle stays centralized.
pub struct PipeWireWaylandCapture;

impl PipeWireWaylandCapture {
    fn portal_available() -> bool {
        std::path::Path::new("/usr/share/xdg-desktop-portal").exists()
            || std::env::var_os("XDG_CURRENT_DESKTOP").is_some()
    }

    fn pipewire_running() -> bool {
        if let Ok(runtime) = std::env::var("XDG_RUNTIME_DIR") {
            if std::path::Path::new(&runtime).join("pipewire-0").exists() {
                return true;
            }
        }
        which("pw-cli").is_some()
    }
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

impl ScreenCaptureEngine for PipeWireWaylandCapture {
    fn list_sources(&self) -> Result<Vec<ScreenSourceInfo>, String> {
        if !Self::pipewire_running() {
            return Err(
                "PipeWire is not available; install pipewire and ensure the session socket is active"
                    .into(),
            );
        }
        let mut sources = Vec::new();
        if let Some(pw_cli) = which("pw-cli") {
            let output = std::process::Command::new(pw_cli)
                .args(["list-objects"])
                .output()
                .map_err(|error| format!("pw-cli list-objects failed: {error}"))?;
            let text = String::from_utf8_lossy(&output.stdout);
            for line in text.lines() {
                if line.contains("Node") && (line.contains("Screen") || line.contains("output")) {
                    sources.push(ScreenSourceInfo {
                        id: format!("pw:{}", sources.len()),
                        name: line.trim().to_string(),
                        source_type: "pipewire-node".into(),
                        resolution: "native".into(),
                    });
                }
            }
        }
        if sources.is_empty() && Self::portal_available() {
            sources.push(ScreenSourceInfo {
                id: "portal:screencast".into(),
                name: "xdg-desktop-portal ScreenCast".into(),
                source_type: "portal".into(),
                resolution: "negotiated".into(),
            });
        }
        if sources.is_empty() {
            return Err(
                "No PipeWire screen sources were enumerated; use monitor selection via wf-recorder"
                    .into(),
            );
        }
        Ok(sources)
    }

    fn start_capture(&self, source_id: &str) -> Result<u32, String> {
        if source_id.trim().is_empty() {
            return Err("Capture source id is required".into());
        }
        if !Self::pipewire_running() && !Self::portal_available() {
            return Err("PipeWire portal capture is unavailable on this session".into());
        }
        // Capture is owned by the projection session helpers (wf-recorder /
        // lab senders). Returning a fabricated stream id would lie to callers.
        Err(
            "PipeWire portal frame ownership is delegated to the active projection session; refuse fabricated stream IDs"
                .into(),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refuses_empty_capture_source() {
        let engine = PipeWireWaylandCapture;
        assert!(engine.start_capture("").is_err());
    }
}
