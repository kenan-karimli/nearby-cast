pub mod audio;
pub mod capture;
pub mod discovery;
pub mod identity;
pub mod nearby_protocol;
pub mod pairing;
pub mod receivers;
pub mod reconnect;
pub mod router;
pub mod security;
pub mod state;
pub mod webrtc;

use discovery::DiscoveryService;
use serde::{Deserialize, Serialize};
use std::io::{BufRead, BufReader, Read};
use std::net::{IpAddr, UdpSocket};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use tauri::{Emitter, Manager, State};

/// Parameters required to rebuild a cast session after an unexpected drop.
#[derive(Debug, Clone)]
pub struct CastSessionSpec {
    pub target_ip: String,
    pub target_port: u16,
    pub output_name: String,
    pub geometry: String,
    pub protocol: String,
    pub audio_mode: String,
    pub pairing_code: Option<String>,
}

pub struct AppState {
    pub discovery: Arc<Mutex<DiscoveryService>>,
    pub cast_child: Mutex<Option<Child>>,
    /// Directory owned by the active launcher and its media children. It is
    /// created with 0700 permissions and removed only by its owner.
    pub cast_session_dir: Mutex<Option<PathBuf>>,
    /// The protocol owned by `cast_child`; avoids treating a running helper as
    /// proof that receiver media playback has started.
    pub cast_protocol: Mutex<Option<String>>,
    pub recv_child: Mutex<Option<Child>>,
    pub nearby_receiver: Mutex<Option<nearby_protocol::NearbyReceiver>>,
    pub nearby_sender: Mutex<Option<nearby_protocol::NearbySenderSession>>,
    /// Last successful cast request, used only for bounded reconnect.
    pub cast_spec: Mutex<Option<CastSessionSpec>>,
    pub reconnect: Mutex<reconnect::ReconnectPolicy>,
    /// When true, stop_projection was requested and reconnect must not run.
    pub stop_requested: Mutex<bool>,
}

const CAST_STATUS_FILE: &str = "status.json";

fn cast_launcher_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    if let Ok(path) = std::env::var("CAST_LAUNCHER") {
        let path = PathBuf::from(path);
        if path.is_file() {
            return Ok(path);
        }
        return Err(format!("CAST_LAUNCHER does not exist: {}", path.display()));
    }

    let development_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(|directory| directory.join("cast_launcher.py"))
        .ok_or_else(|| "Could not determine the development launcher path".to_string())?;
    if development_path.is_file() {
        return Ok(development_path);
    }

    if let Ok(resource_dir) = app.path().resource_dir() {
        let resource_path = resource_dir.join("cast_launcher.py");
        if resource_path.is_file() {
            return Ok(resource_path);
        }
    }

    let installed_path = PathBuf::from("/usr/share/nearby-cast/cast_launcher.py");
    if installed_path.is_file() {
        return Ok(installed_path);
    }

    Err(
        "cast_launcher.py was not found. Set CAST_LAUNCHER or install the application resources."
            .to_string(),
    )
}

fn write_helper_status(session_dir: &Path, state: &str, message: &str) {
    let path = session_dir.join(CAST_STATUS_FILE);
    let temporary = session_dir.join("status.json.tmp");
    let payload = serde_json::json!({
        "state": state,
        "message": message,
        "updated_at": std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs(),
    });
    if let Ok(value) = serde_json::to_vec(&payload) {
        let _ = std::fs::write(&temporary, value);
        let _ = std::fs::rename(temporary, path);
    }
}

fn forward_launcher_output<R>(
    stream: R,
    stream_name: &'static str,
    app: tauri::AppHandle,
    miracast_status_dir: Option<PathBuf>,
) where
    R: Read + Send + 'static,
{
    std::thread::spawn(move || {
        for line in BufReader::new(stream).lines().map_while(Result::ok) {
            println!("[CAST_LAUNCHER:{stream_name}] {line}");
            if let Some(directory) = miracast_status_dir.as_ref() {
                if line.contains("PLAY accepted; media stream started") {
                    write_helper_status(
                        directory,
                        "casting",
                        "Miracast receiver accepted RTP media",
                    );
                } else if line.contains("AirPlay lab receiver accepted mirroring")
                    || line.contains("[STATUS] casting:")
                {
                    write_helper_status(
                        directory,
                        "casting",
                        "AirPlay lab receiver accepted mirroring",
                    );
                } else if line.contains("TV connected") || line.contains("Starting media as") {
                    write_helper_status(
                        directory,
                        "negotiating",
                        "Miracast receiver is negotiating the media session",
                    );
                } else if line.contains("[FluxCast WFD] ERROR:")
                    || line.contains("[FluxCast WFD RTSP] ERROR:")
                    || line.contains("[FluxCast] ERROR:")
                {
                    write_helper_status(directory, "failed", line.trim());
                }
            }
            let _ = app.emit(
                "cast_log",
                serde_json::json!({
                    "stream": stream_name,
                    "message": line,
                }),
            );
        }
    });
}

fn terminate_cast_child(cast: &mut Option<Child>) {
    if let Some(child) = cast.as_mut() {
        let _ = Command::new("kill")
            .args(["-INT", &child.id().to_string()])
            .status();
        let _ = child.wait();
    }
    *cast = None;
}

fn create_cast_session_dir() -> Result<PathBuf, String> {
    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;

    for _ in 0..3 {
        let directory =
            std::env::temp_dir().join(format!("nearby-cast-{:032x}", rand::random::<u128>()));
        match std::fs::create_dir(&directory) {
            Ok(()) => {
                #[cfg(unix)]
                std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700))
                    .map_err(|error| format!("Could not secure cast session directory: {error}"))?;
                return Ok(directory);
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(format!("Could not create cast session directory: {error}")),
        }
    }
    Err("Could not allocate a unique cast session directory".into())
}

fn clear_cast_session_dir(state: &AppState) {
    let directory = state.cast_session_dir.lock().unwrap().take();
    if let Some(directory) = directory {
        let _ = std::fs::remove_dir_all(directory);
    }
}

fn cast_status_path(state: &AppState) -> Option<PathBuf> {
    state
        .cast_session_dir
        .lock()
        .unwrap()
        .as_ref()
        .map(|directory| directory.join(CAST_STATUS_FILE))
}

fn lab_mode_enabled() -> bool {
    ["NEARBY_CAST_VIRTUAL_LAB", "NEARBY_CAST_ALLOW_LOOPBACK"]
        .iter()
        .any(|key| {
            std::env::var(key)
                .ok()
                .map(|value| matches!(value.to_ascii_lowercase().as_str(), "1" | "true" | "yes"))
                .unwrap_or(false)
        })
}

fn parse_routable_ip(value: &str) -> Result<IpAddr, String> {
    let address = value
        .parse::<IpAddr>()
        .map_err(|_| "Enter a valid IPv4 or IPv6 receiver address.".to_string())?;
    let allow_loopback = lab_mode_enabled();
    if address.is_unspecified() || address.is_multicast() {
        return Err("The receiver address must be a routable unicast address.".to_string());
    }
    if address.is_loopback() && !allow_loopback {
        return Err("The receiver address must be a routable unicast address.".to_string());
    }
    if !address.is_ipv4() {
        return Err(
            "Google Cast streaming currently requires an IPv4 receiver address.".to_string(),
        );
    }
    Ok(address)
}

fn looks_like_ipv4(value: &str) -> bool {
    value.parse::<std::net::Ipv4Addr>().is_ok()
}

fn parse_video_size(geometry: &str) -> (u32, u32) {
    // Accept "1920x1080", "100,200 1920x1080", or "WxH+X+Y".
    for token in geometry.split(|c: char| c.is_whitespace() || c == ',') {
        let lowered = token.to_ascii_lowercase();
        let dims = lowered.split('+').next().unwrap_or(&lowered);
        if let Some((w, h)) = dims.split_once('x') {
            if let (Ok(width), Ok(height)) = (w.parse::<u32>(), h.parse::<u32>()) {
                if width >= 16 && height >= 16 && width <= 7680 && height <= 4320 {
                    return (width, height);
                }
            }
        }
    }
    (1920, 1080)
}

/// Wayland display output monitor info
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WlOutput {
    pub name: String,
    pub description: String,
    pub resolution: String,
}

/// Open application window info for window sharing selection
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WlWindow {
    pub id: String,
    pub title: String,
    pub class_name: String,
    pub geometry: String,
}

/// Discovered display target for UI — includes protocol and capability fields
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DisplayTarget {
    pub id: String,
    pub name: String,
    pub platform: String,
    pub ip_address: String,
    pub port: u16,
    pub status: String,
    pub protocol: String,
    pub screen_cast: bool,
    pub media_cast: bool,
    pub audio_cast: bool,
    pub requires_pin: bool,
    pub support_notes: String,
}

// ─── Tauri IPC Commands ───────────────────────────────────────────────────────

/// Fetch all nearby displays discovered via mDNS/SSDP
#[tauri::command]
fn get_discovered_devices(state: State<AppState>) -> Vec<DisplayTarget> {
    let disc = state.discovery.lock().unwrap();
    disc.list_devices()
        .into_iter()
        .map(|d| DisplayTarget {
            id: d.id,
            name: d.name,
            platform: d.platform,
            ip_address: d.ip_address,
            port: d.port,
            status: d.status,
            protocol: d.protocol.to_string(),
            screen_cast: d.device_capabilities.screen_cast,
            media_cast: d.device_capabilities.media_cast,
            audio_cast: d.device_capabilities.audio_cast,
            requires_pin: d.device_capabilities.requires_pin,
            support_notes: d.device_capabilities.support_notes,
        })
        .collect()
}

/// Active Wi-Fi Direct discovery is separate from mDNS: WFD peers commonly
/// exist only on the P2P radio and are not normal LAN services. FluxCast
/// returns only peers whose WFD information element was observed.
#[tauri::command]
fn scan_miracast_devices() -> serde_json::Value {
    let Some(helper) = receivers::miracast::fluxcast_path() else {
        return serde_json::json!({
            "ok": false,
            "error": "Install FluxCast to scan Wi-Fi Direct Miracast displays.",
            "devices": [],
        });
    };
    let output = match Command::new(helper)
        .args(["--wfd-scan", "--wfd-timeout", "8"])
        .env("PYTHONUNBUFFERED", "1")
        .output()
    {
        Ok(output) => output,
        Err(error) => {
            return serde_json::json!({ "ok": false, "error": format!("Could not start Wi-Fi Direct discovery: {error}"), "devices": [] })
        }
    };
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    let devices = parse_fluxcast_wfd_scan(&stdout);
    if !output.status.success() {
        let detail = stdout
            .lines()
            .chain(stderr.lines())
            .find(|line| line.contains("ERROR:"))
            .unwrap_or("Wi-Fi Direct discovery failed; check NetworkManager P2P permissions.");
        return serde_json::json!({ "ok": false, "error": detail.trim(), "devices": devices });
    }
    serde_json::json!({ "ok": true, "devices": devices })
}

fn parse_fluxcast_wfd_scan(output: &str) -> Vec<DisplayTarget> {
    let mut devices = Vec::new();
    let mut candidate: Option<(String, String)> = None;
    for line in output.lines() {
        let trimmed = line.trim();
        if let Some(rest) = trimmed
            .strip_prefix('[')
            .and_then(|value| value.split_once("] ").map(|(_, value)| value))
        {
            if let Some((mac, tail)) = rest.split_once(' ') {
                if receivers::miracast::valid_peer_selector(mac) && mac.len() == 17 {
                    let name = tail.split(" via ").next().unwrap_or("").trim();
                    candidate = Some((
                        mac.to_ascii_lowercase(),
                        if name.is_empty() {
                            "Wireless display".into()
                        } else {
                            name.into()
                        },
                    ));
                }
            }
        } else if trimmed.contains("WFD capability data detected") {
            if let Some((mac, name)) = candidate.take() {
                devices.push(DisplayTarget {
                    id: format!("wfd:{mac}"),
                    name,
                    platform: "Wi-Fi Direct".into(),
                    ip_address: mac,
                    port: 7236,
                    status: "available".into(),
                    protocol: "Miracast".into(),
                    screen_cast: true,
                    media_cast: false,
                    audio_cast: true,
                    requires_pin: false,
                    support_notes: "Wi-Fi Direct WFD capability verified; session negotiation still occurs when you connect.".into(),
                });
            }
        }
    }
    devices
}

/// Detect local displays (eDP-1, HDMI-A-1, etc.)
#[tauri::command]
fn list_outputs() -> Vec<WlOutput> {
    // 1. Try wlr-randr (Hyprland / wlroots)
    if let Ok(out) = Command::new("wlr-randr").output() {
        let raw = String::from_utf8_lossy(&out.stdout).to_string();
        let mut outputs = Vec::new();
        let mut current_name = String::new();
        let mut current_desc = String::new();
        let mut current_res = String::new();
        for line in raw.lines() {
            if !line.starts_with(' ') && !line.is_empty() {
                if !current_name.is_empty() {
                    outputs.push(WlOutput {
                        name: current_name.clone(),
                        description: current_desc.clone(),
                        resolution: current_res.clone(),
                    });
                }
                let parts: Vec<&str> = line.splitn(2, ' ').collect();
                current_name = parts[0].trim().to_string();
                current_desc = parts.get(1).unwrap_or(&"").trim().to_string();
                current_res = "resolution unavailable".to_string();
            } else if line.contains("current") && line.contains("px") {
                if let Some(res) = line.split_whitespace().next() {
                    current_res = res.to_string();
                }
            }
        }
        if !current_name.is_empty() {
            outputs.push(WlOutput {
                name: current_name,
                description: current_desc,
                resolution: current_res,
            });
        }
        if !outputs.is_empty() {
            return outputs;
        }
    }

    // 2. Try wf-recorder --list-output
    if let Ok(out) = Command::new("wf-recorder").arg("--list-output").output() {
        let raw = String::from_utf8_lossy(&out.stdout).to_string();
        let outputs: Vec<WlOutput> = raw
            .lines()
            .filter(|l| !l.trim().is_empty())
            .map(|l| WlOutput {
                name: l.trim().to_string(),
                description: "Wayland Display".to_string(),
                resolution: "resolution unavailable".to_string(),
            })
            .collect();
        if !outputs.is_empty() {
            return outputs;
        }
    }

    Vec::new()
}

/// Enumerate open application windows for window-specific sharing
#[tauri::command]
fn list_windows() -> Vec<WlWindow> {
    if let Ok(out) = Command::new("hyprctl").args(["clients", "-j"]).output() {
        let raw = String::from_utf8_lossy(&out.stdout).to_string();
        if let Ok(json) = serde_json::from_str::<serde_json::Value>(&raw) {
            if let Some(arr) = json.as_array() {
                let mut windows = Vec::new();
                for (idx, win) in arr.iter().enumerate() {
                    let title = win
                        .get("title")
                        .and_then(|v| v.as_str())
                        .unwrap_or("Window")
                        .to_string();
                    let class = win
                        .get("class")
                        .and_then(|v| v.as_str())
                        .unwrap_or("App")
                        .to_string();
                    let at = win.get("at").and_then(|v| v.as_array());
                    let size = win.get("size").and_then(|v| v.as_array());

                    if title.is_empty() || class.is_empty() {
                        continue;
                    }

                    if let (Some(a), Some(s)) = (at, size) {
                        let x = a.first().and_then(|v| v.as_i64()).unwrap_or(0);
                        let y = a.get(1).and_then(|v| v.as_i64()).unwrap_or(0);
                        let w = s.first().and_then(|v| v.as_i64()).unwrap_or(1920);
                        let h = s.get(1).and_then(|v| v.as_i64()).unwrap_or(1080);
                        let geom = format!("{},{} {}x{}", x, y, w, h);
                        windows.push(WlWindow {
                            id: format!("win-{}", idx),
                            title,
                            class_name: class,
                            geometry: geom,
                        });
                    }
                }
                if !windows.is_empty() {
                    return windows;
                }
            }
        }
    }
    Vec::new()
}

/// Interactively select a region on screen using slurp
#[tauri::command]
fn select_region_slurp() -> serde_json::Value {
    match Command::new("slurp").output() {
        Ok(out) => {
            let geom = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if !geom.is_empty() {
                serde_json::json!({ "ok": true, "geometry": geom })
            } else {
                serde_json::json!({ "ok": false, "error": "Selection cancelled" })
            }
        }
        Err(e) => serde_json::json!({ "ok": false, "error": e.to_string() }),
    }
}

/// Run diagnostics on a receiver. Google Cast uses LAN TCP/DIAL probes.
/// Miracast Wi-Fi Direct peers are identified by MAC and need FluxCast/WFD checks.
#[tauri::command]
fn diagnose_device(target_ip: String) -> serde_json::Value {
    use std::net::TcpStream;
    use std::time::Duration;

    let trimmed = target_ip.trim().to_string();
    if receivers::miracast::is_mac_address(&trimmed) {
        return diagnose_miracast_peer(&trimmed);
    }

    let target_ip = match parse_routable_ip(&trimmed) {
        Ok(address) => address.to_string(),
        Err(error) => return serde_json::json!({ "error": error, "checks": [] }),
    };
    let mut results: Vec<serde_json::Value> = Vec::new();

    let reachable = TcpStream::connect_timeout(
        &format!("{}:80", target_ip)
            .parse()
            .unwrap_or_else(|_| "0.0.0.0:80".parse().unwrap()),
        Duration::from_secs(2),
    )
    .is_ok();
    results.push(serde_json::json!({ "check": "IP Reachable", "ok": reachable }));

    for port in [8008u16, 8009u16] {
        let ok = TcpStream::connect_timeout(
            &format!("{}:{}", target_ip, port)
                .parse()
                .unwrap_or_else(|_| "0.0.0.0:80".parse().unwrap()),
            Duration::from_secs(2),
        )
        .is_ok();
        results
            .push(serde_json::json!({ "check": format!("TCP :{} (Google Cast)", port), "ok": ok }));
    }

    let dial_url = format!("http://{}:8008/ssdp/device-desc.xml", target_ip);
    let dial_ok = Command::new("curl")
        .args([
            "-s",
            "--max-time",
            "3",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            &dial_url,
        ])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim() == "200")
        .unwrap_or(false);
    results.push(
        serde_json::json!({ "check": "DIAL /ssdp/device-desc.xml (Google Cast)", "ok": dial_ok }),
    );

    let py_check = Command::new("python3")
        .args([
            "-c",
            "import pychromecast, sys; cs, b = pychromecast.get_chromecasts(known_hosts=[sys.argv[1]]); pychromecast.discovery.stop_discovery(b); sys.exit(0 if cs else 1)",
            &target_ip,
        ])
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    results.push(serde_json::json!({ "check": "pychromecast: Chromecast found", "ok": py_check }));

    let protocol = if dial_ok || py_check {
        "Google Cast"
    } else {
        "Unknown"
    };

    println!(
        "[DIAGNOSE] Device {} — protocol={} dial_ok={} cast_reachable={}",
        target_ip, protocol, dial_ok, py_check
    );

    serde_json::json!({
        "target": target_ip,
        "protocol_detected": protocol,
        "checks": results,
    })
}

fn diagnose_miracast_peer(peer: &str) -> serde_json::Value {
    let mut checks = Vec::new();
    let helper = receivers::miracast::fluxcast_path();
    let fluxcast_ok = helper.is_some();
    checks.push(serde_json::json!({
        "check": "FluxCast WFD helper",
        "ok": fluxcast_ok,
        "detail": if fluxcast_ok {
            helper.as_ref().map(|path| path.display().to_string()).unwrap_or_default()
        } else {
            "Install FluxCast or set NEARBY_CAST_FLUXCAST".into()
        }
    }));

    let nmcli_ok = Command::new("nmcli")
        .args(["-t", "-f", "DEVICE,TYPE,STATE", "device", "status"])
        .output()
        .map(|output| {
            output.status.success()
                && String::from_utf8_lossy(&output.stdout)
                    .lines()
                    .any(|line| line.contains(":wifi-p2p:"))
        })
        .unwrap_or(false);
    checks.push(serde_json::json!({
        "check": "NetworkManager Wi-Fi Direct device",
        "ok": nmcli_ok,
        "detail": if nmcli_ok {
            "wifi-p2p device present"
        } else {
            "No wifi-p2p device reported by nmcli"
        }
    }));

    checks.push(serde_json::json!({
        "check": "WFD peer selector",
        "ok": true,
        "detail": peer
    }));

    let protocol = if fluxcast_ok { "Miracast" } else { "Unknown" };
    serde_json::json!({
        "target": peer,
        "protocol_detected": protocol,
        "checks": checks,
        "note": "Miracast peers use Wi-Fi Direct. LAN TCP/DIAL probes do not apply.",
    })
}

/// Start screen projection — supports optional geometry and audio_mode ('system' | 'silent')
#[allow(clippy::too_many_arguments)] // Tauri command arguments define the IPC contract.
#[tauri::command]
fn start_projection(
    target_ip: String,
    target_port: u16,
    output_name: String,
    geometry: String,
    protocol: String,
    audio_mode: Option<String>,
    pairing_code: Option<String>,
    state: State<AppState>,
    app: tauri::AppHandle,
) -> serde_json::Value {
    if !matches!(
        audio_mode.as_deref(),
        None | Some("system") | Some("silent")
    ) {
        return serde_json::json!({ "ok": false, "error": "Unsupported audio mode." });
    }
    let normalized_protocol = protocol.to_ascii_lowercase().replace(' ', "_");
    if !matches!(
        normalized_protocol.as_str(),
        "google_cast" | "miracast" | "nearby_cast" | "airplay"
    ) {
        return serde_json::json!({
            "ok": false,
            "error": "The selected receiver protocol is not supported for screen casting.",
            "protocol": protocol,
        });
    }
    let target_ip = if normalized_protocol == "miracast" {
        if !receivers::miracast::valid_peer_selector(&target_ip) {
            return serde_json::json!({ "ok": false, "error": "The selected wireless display has an invalid Wi-Fi Direct peer identifier." });
        }
        target_ip.trim().to_string()
    } else {
        match parse_routable_ip(&target_ip) {
            Ok(address) => address.to_string(),
            Err(error) => return serde_json::json!({ "ok": false, "error": error }),
        }
    };
    {
        let mut cast = state.cast_child.lock().unwrap();
        terminate_cast_child(&mut cast);
    }
    {
        let mut sender = state.nearby_sender.lock().unwrap();
        if let Some(session) = sender.as_mut() {
            session.stop();
        }
        *sender = None;
    }
    clear_cast_session_dir(&state);
    let session_dir = match create_cast_session_dir() {
        Ok(directory) => directory,
        Err(error) => return serde_json::json!({ "ok": false, "error": error }),
    };
    let session_token = format!("{:032x}", rand::random::<u128>());
    *state.stop_requested.lock().unwrap() = false;
    {
        let mut policy = state.reconnect.lock().unwrap();
        if !policy.is_active() {
            policy.reset();
        }
    }

    let mode = audio_mode.unwrap_or_else(|| "system".to_string());
    let cast_spec = CastSessionSpec {
        target_ip: target_ip.clone(),
        target_port,
        output_name: output_name.clone(),
        geometry: geometry.clone(),
        protocol: normalized_protocol.clone(),
        audio_mode: mode.clone(),
        pairing_code: pairing_code.clone(),
    };
    println!(
        "[CONNECTION] start_projection target={} protocol={} output={} geometry='{}' audio={}",
        target_ip, protocol, output_name, geometry, mode
    );

    if normalized_protocol == "nearby_cast" {
        let config_dir = nearby_protocol::config_dir();
        let identity =
            match identity::IdentityManager::new(config_dir.clone()).get_or_create_identity() {
                Ok(identity) => identity,
                Err(error) => {
                    let _ = std::fs::remove_dir_all(&session_dir);
                    return serde_json::json!({ "ok": false, "error": error });
                }
            };
        let control_port = if target_port == 0 { 29870 } else { target_port };
        let (width, height) = parse_video_size(&geometry);
        write_helper_status(
            &session_dir,
            "starting",
            "Negotiating authenticated NearbyCast session",
        );
        match nearby_protocol::NearbySenderSession::start(nearby_protocol::NearbySenderRequest {
            target_ip: &target_ip,
            control_port,
            output_name: &output_name,
            audio_mode: &mode,
            local_identity: &identity,
            session_dir: &session_dir,
            pairing_code: pairing_code.as_deref(),
            video_width: width,
            video_height: height,
        }) {
            Ok(session) => {
                write_helper_status(
                    &session_dir,
                    "casting",
                    "NearbyCast receiver accepted the authenticated media session",
                );
                *state.nearby_sender.lock().unwrap() = Some(session);
                *state.cast_session_dir.lock().unwrap() = Some(session_dir);
                *state.cast_protocol.lock().unwrap() = Some("Nearby Cast".into());
                *state.cast_spec.lock().unwrap() = Some(cast_spec);
                return serde_json::json!({
                    "ok": true,
                    "target": target_ip,
                    "protocol": "Nearby Cast",
                });
            }
            Err(error) => {
                let _ = std::fs::remove_dir_all(&session_dir);
                return serde_json::json!({ "ok": false, "error": error });
            }
        }
    }

    if normalized_protocol == "airplay" {
        let Some(helper) = receivers::airplay::airplay_lab_sender_path() else {
            let _ = std::fs::remove_dir_all(&session_dir);
            return serde_json::json!({
                "ok": false,
                "error": "AirPlay lab sender is missing from this checkout.",
            });
        };
        write_helper_status(
            &session_dir,
            "starting",
            "Preparing AirPlay lab mirroring session (FairPlay Apple TV NOT VERIFIED)",
        );
        let http_port = if target_port == 0 {
            "7000".to_string()
        } else {
            target_port.to_string()
        };
        let pin = pairing_code.clone().unwrap_or_else(|| "0000".to_string());
        let mut command = Command::new("python3");
        command
            .arg(&helper)
            .arg("--target")
            .arg(&target_ip)
            .arg("--http-port")
            .arg(&http_port)
            .arg("--pin")
            .arg(&pin)
            .arg("--monitor")
            .arg(&output_name)
            .arg("--session-dir")
            .arg(&session_dir)
            .env("PYTHONUNBUFFERED", "1")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if mode == "silent" {
            command.arg("--no-audio");
        }
        if lab_mode_enabled()
            || std::env::var("NEARBY_CAST_LAB_MEDIA")
                .map(|v| matches!(v.to_ascii_lowercase().as_str(), "1" | "true" | "yes"))
                .unwrap_or(false)
        {
            command.arg("--lab-media");
            command.env("NEARBY_CAST_LAB_MEDIA", "1");
        }
        match command.spawn() {
            Ok(mut child) => {
                if let Some(stdout) = child.stdout.take() {
                    forward_launcher_output(
                        stdout,
                        "stdout",
                        app.clone(),
                        Some(session_dir.clone()),
                    );
                }
                if let Some(stderr) = child.stderr.take() {
                    forward_launcher_output(stderr, "stderr", app, Some(session_dir.clone()));
                }
                let pid = child.id();
                *state.cast_child.lock().unwrap() = Some(child);
                *state.cast_session_dir.lock().unwrap() = Some(session_dir);
                *state.cast_protocol.lock().unwrap() = Some("AirPlay".into());
                *state.cast_spec.lock().unwrap() = Some(cast_spec);
                return serde_json::json!({
                    "ok": true,
                    "pid": pid,
                    "target": target_ip,
                    "protocol": "AirPlay",
                    "transport": "airplay-lab-rtsp",
                    "physical_apple": "NOT VERIFIED",
                });
            }
            Err(error) => {
                let _ = std::fs::remove_dir_all(&session_dir);
                return serde_json::json!({
                    "ok": false,
                    "error": format!("Could not start the AirPlay lab sender: {error}")
                });
            }
        }
    }

    if normalized_protocol == "miracast" {
        // IPv4 targets are lab/WFD-over-LAN sinks (no Wi-Fi Direct MAC). Route
        // them through the production lab sender so RTSP/RTP can be verified
        // without pretending P2P association happened.
        if looks_like_ipv4(&target_ip) {
            let Some(helper) = receivers::miracast::wfd_lab_sender_path() else {
                let _ = std::fs::remove_dir_all(&session_dir);
                return serde_json::json!({
                    "ok": false,
                    "error": "Miracast lab sender is missing from this checkout.",
                });
            };
            write_helper_status(
                &session_dir,
                "starting",
                "Preparing Miracast WFD lab RTSP session (physical P2P NOT VERIFIED)",
            );
            let mut command = Command::new("python3");
            command
                .arg(&helper)
                .arg("--target")
                .arg(&target_ip)
                .arg("--rtsp-port")
                .arg(if target_port == 0 {
                    "7236".to_string()
                } else {
                    target_port.to_string()
                })
                .arg("--monitor")
                .arg(&output_name)
                .arg("--session-dir")
                .arg(&session_dir)
                .env("PYTHONUNBUFFERED", "1")
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            if mode == "silent" {
                command.arg("--no-audio");
            }
            match command.spawn() {
                Ok(mut child) => {
                    if let Some(stdout) = child.stdout.take() {
                        forward_launcher_output(
                            stdout,
                            "stdout",
                            app.clone(),
                            Some(session_dir.clone()),
                        );
                    }
                    if let Some(stderr) = child.stderr.take() {
                        forward_launcher_output(stderr, "stderr", app, Some(session_dir.clone()));
                    }
                    let pid = child.id();
                    *state.cast_child.lock().unwrap() = Some(child);
                    *state.cast_session_dir.lock().unwrap() = Some(session_dir);
                    *state.cast_protocol.lock().unwrap() = Some("Miracast".into());
                    *state.cast_spec.lock().unwrap() = Some(cast_spec);
                    return serde_json::json!({
                        "ok": true,
                        "pid": pid,
                        "target": target_ip,
                        "protocol": "Miracast",
                        "transport": "wfd-lab-rtsp",
                        "physical_p2p": "NOT VERIFIED",
                    });
                }
                Err(error) => {
                    let _ = std::fs::remove_dir_all(&session_dir);
                    return serde_json::json!({
                        "ok": false,
                        "error": format!("Could not start the Miracast lab sender: {error}")
                    });
                }
            }
        }

        let Some(helper) = receivers::miracast::fluxcast_path() else {
            let _ = std::fs::remove_dir_all(&session_dir);
            return serde_json::json!({ "ok": false, "error": "Miracast needs the FluxCast WFD sender. Install FluxCast, then retry." });
        };
        write_helper_status(
            &session_dir,
            "starting",
            "Preparing Wi-Fi Direct and WFD RTSP session",
        );
        let mut command = Command::new(helper);
        command
            .args([
                "--protocol",
                "wfd",
                "--wfd-peer",
                &target_ip,
                "--wfd-ffmpeg-stats",
                "--wfd-capture-backend",
                "auto",
            ])
            .arg("--wfd-latency-log")
            .arg(session_dir.join("miracast-latency.jsonl"))
            .arg("--monitor")
            .arg(&output_name)
            .env("PYTHONUNBUFFERED", "1")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if mode == "silent" {
            command.arg("--wfd-no-audio");
        }
        match command.spawn() {
            Ok(mut child) => {
                if let Some(stdout) = child.stdout.take() {
                    forward_launcher_output(
                        stdout,
                        "stdout",
                        app.clone(),
                        Some(session_dir.clone()),
                    );
                }
                if let Some(stderr) = child.stderr.take() {
                    forward_launcher_output(stderr, "stderr", app, Some(session_dir.clone()));
                }
                let pid = child.id();
                *state.cast_child.lock().unwrap() = Some(child);
                *state.cast_session_dir.lock().unwrap() = Some(session_dir);
                *state.cast_protocol.lock().unwrap() = Some("Miracast".into());
                *state.cast_spec.lock().unwrap() = Some(cast_spec);
                return serde_json::json!({ "ok": true, "pid": pid, "target": target_ip, "protocol": "Miracast" });
            }
            Err(error) => {
                let _ = std::fs::remove_dir_all(&session_dir);
                return serde_json::json!({ "ok": false, "error": format!("Could not start the Miracast sender: {error}") });
            }
        }
    }

    let script = match cast_launcher_path(&app) {
        Ok(path) => path,
        Err(error) => {
            let _ = std::fs::remove_dir_all(&session_dir);
            return serde_json::json!({ "ok": false, "error": error });
        }
    };
    let child_result = {
        let mut command = Command::new("python3");
        command
            .arg(&script)
            .arg(&target_ip)
            .arg(&output_name)
            .arg(&geometry)
            .arg(&mode)
            .env("NEARBY_CAST_SESSION_DIR", &session_dir)
            .env("NEARBY_CAST_SESSION_TOKEN", &session_token)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if lab_mode_enabled() {
            command.env("NEARBY_CAST_VIRTUAL_LAB", "1");
            command.env("NEARBY_CAST_ALLOW_LOOPBACK", "1");
            if let Ok(load) = std::env::var("NEARBY_CAST_LAB_CAST_LOAD") {
                command.env("NEARBY_CAST_LAB_CAST_LOAD", load);
            }
            if std::env::var("NEARBY_CAST_LAB_MEDIA")
                .map(|v| matches!(v.to_ascii_lowercase().as_str(), "1" | "true" | "yes"))
                .unwrap_or(false)
            {
                command.env("NEARBY_CAST_LAB_MEDIA", "1");
            }
        }
        match command.spawn() {
            Ok(mut child) => {
                if let Some(stdout) = child.stdout.take() {
                    forward_launcher_output(stdout, "stdout", app.clone(), None);
                }
                if let Some(stderr) = child.stderr.take() {
                    forward_launcher_output(stderr, "stderr", app.clone(), None);
                }
                Ok(child)
            }
            Err(error) => Err(error),
        }
    };

    match child_result {
        Ok(child) => {
            let pid = child.id();
            *state.cast_child.lock().unwrap() = Some(child);
            *state.cast_session_dir.lock().unwrap() = Some(session_dir);
            *state.cast_protocol.lock().unwrap() = Some("Google Cast".into());
            *state.cast_spec.lock().unwrap() = Some(cast_spec);
            println!("[STREAM] Projection process started (PID {})", pid);
            serde_json::json!({ "ok": true, "pid": pid, "target": target_ip, "protocol": protocol })
        }
        Err(e) => {
            let _ = std::fs::remove_dir_all(&session_dir);
            println!("[ERROR] Projection failed: {}", e);
            serde_json::json!({ "ok": false, "error": e.to_string() })
        }
    }
}

/// Stop active projection — also called on app exit
#[tauri::command]
fn stop_projection(state: State<AppState>) -> bool {
    *state.stop_requested.lock().unwrap() = true;
    state.reconnect.lock().unwrap().reset();
    *state.cast_spec.lock().unwrap() = None;
    let mut cast = state.cast_child.lock().unwrap();
    terminate_cast_child(&mut cast);
    drop(cast);
    {
        let mut sender = state.nearby_sender.lock().unwrap();
        if let Some(session) = sender.as_mut() {
            session.stop();
        }
        *sender = None;
    }
    clear_cast_session_dir(&state);
    *state.cast_protocol.lock().unwrap() = None;
    true
}

fn schedule_reconnect(state: &AppState, reason: &str) -> Option<serde_json::Value> {
    if *state.stop_requested.lock().unwrap() {
        return None;
    }
    let spec = state.cast_spec.lock().unwrap().clone()?;
    let mut policy = state.reconnect.lock().unwrap();
    if policy.is_exhausted() {
        let snap = policy.snapshot();
        return Some(serde_json::json!({
            "state": "failed",
            "message": format!("Reconnect exhausted: {reason}"),
            "reconnect": snap,
            "target": spec.target_ip,
            "protocol": spec.protocol,
        }));
    }
    if !policy.is_active() && !policy.begin(reason) {
        let snap = policy.snapshot();
        return Some(serde_json::json!({
            "state": "failed",
            "message": format!("Reconnect exhausted: {reason}"),
            "reconnect": snap,
            "target": spec.target_ip,
            "protocol": spec.protocol,
        }));
    }
    let snap = policy.snapshot();
    Some(serde_json::json!({
        "state": "reconnecting",
        "message": format!(
            "Reconnecting to {} (attempt {}/{})",
            spec.target_ip, snap.attempt, snap.max_attempts
        ),
        "reconnect": snap,
        "target": spec.target_ip,
        "protocol": spec.protocol,
    }))
}

fn reconnect_status(state: &AppState) -> Option<serde_json::Value> {
    let policy = state.reconnect.lock().unwrap();
    if !policy.is_active() && !policy.is_exhausted() {
        return None;
    }
    let snap = policy.snapshot();
    let target = state
        .cast_spec
        .lock()
        .unwrap()
        .as_ref()
        .map(|spec| spec.target_ip.clone())
        .unwrap_or_default();
    let protocol = state
        .cast_spec
        .lock()
        .unwrap()
        .as_ref()
        .map(|spec| spec.protocol.clone())
        .unwrap_or_default();
    if policy.is_exhausted() {
        return Some(serde_json::json!({
            "state": "failed",
            "message": format!(
                "Reconnect exhausted after {} attempts{}",
                snap.max_attempts,
                snap.last_error
                    .as_ref()
                    .map(|error| format!(": {error}"))
                    .unwrap_or_default()
            ),
            "reconnect": snap,
            "target": target,
            "protocol": protocol,
        }));
    }
    Some(serde_json::json!({
        "state": "reconnecting",
        "message": format!(
            "Reconnecting to {target} (attempt {}/{}, next in {}ms)",
            snap.attempt, snap.max_attempts, snap.next_delay_ms
        ),
        "reconnect": snap,
        "target": target,
        "protocol": protocol,
    }))
}

/// Rebuild the last cast session if a reconnect attempt is due.
#[tauri::command]
fn attempt_reconnect(state: State<AppState>, app: tauri::AppHandle) -> serde_json::Value {
    if *state.stop_requested.lock().unwrap() {
        return serde_json::json!({ "ok": false, "error": "Stop was requested." });
    }
    let ready = {
        let mut policy = state.reconnect.lock().unwrap();
        policy.ready_to_attempt()
    };
    if !ready {
        if let Some(status) = reconnect_status(&state) {
            return serde_json::json!({ "ok": false, "pending": true, "status": status });
        }
        return serde_json::json!({ "ok": false, "error": "No reconnect is pending." });
    }
    let Some(spec) = state.cast_spec.lock().unwrap().clone() else {
        return serde_json::json!({ "ok": false, "error": "No cast session to reconnect." });
    };
    let protocol = match spec.protocol.as_str() {
        "nearby_cast" | "Nearby Cast" => "Nearby Cast",
        "miracast" | "Miracast" => "Miracast",
        "airplay" | "AirPlay" => "AirPlay",
        _ => "Google Cast",
    };
    let result = start_projection(
        spec.target_ip,
        spec.target_port,
        spec.output_name,
        spec.geometry,
        protocol.to_string(),
        Some(spec.audio_mode),
        spec.pairing_code,
        state.clone(),
        app,
    );
    if result
        .get("ok")
        .and_then(|value| value.as_bool())
        .unwrap_or(false)
    {
        state.reconnect.lock().unwrap().mark_recovered();
        serde_json::json!({ "ok": true, "status": result })
    } else {
        let reason = result
            .get("error")
            .and_then(|value| value.as_str())
            .unwrap_or("reconnect failed")
            .to_string();
        let status = {
            let mut policy = state.reconnect.lock().unwrap();
            if !policy.note_failure(&reason) {
                let snap = policy.snapshot();
                serde_json::json!({
                    "state": "failed",
                    "message": format!("Reconnect exhausted: {reason}"),
                    "reconnect": snap,
                })
            } else {
                let snap = policy.snapshot();
                serde_json::json!({
                    "state": "reconnecting",
                    "message": format!(
                        "Reconnect attempt failed; retry {}/{}",
                        snap.attempt, snap.max_attempts
                    ),
                    "reconnect": snap,
                })
            }
        };
        serde_json::json!({ "ok": false, "status": status })
    }
}

#[tauri::command]
fn check_cast_alive(state: State<AppState>) -> serde_json::Value {
    if let Some(status) = reconnect_status(&state) {
        if status.get("state").and_then(|value| value.as_str()) == Some("reconnecting") {
            return status;
        }
        if status.get("state").and_then(|value| value.as_str()) == Some("failed") {
            return status;
        }
    }

    let reported_status = cast_status_path(&state)
        .and_then(|path| std::fs::read_to_string(path).ok())
        .and_then(|contents| serde_json::from_str::<serde_json::Value>(&contents).ok());

    {
        let protocol = state.cast_protocol.lock().unwrap().clone();
        if protocol.as_deref() == Some("Nearby Cast") {
            let mut sender = state.nearby_sender.lock().unwrap();
            let alive = sender.as_mut().is_some_and(|session| {
                session
                    .children
                    .iter_mut()
                    .all(|child| matches!(child.try_wait(), Ok(None)))
            });
            if alive {
                state.reconnect.lock().unwrap().mark_recovered();
                return reported_status.unwrap_or_else(|| {
                    serde_json::json!({
                        "state": "casting",
                        "message": "NearbyCast authenticated session is active",
                    })
                });
            }
            *sender = None;
            *state.cast_protocol.lock().unwrap() = None;
            if let Some(status) = schedule_reconnect(&state, "NearbyCast sender processes exited") {
                return status;
            }
            return serde_json::json!({
                "state": "failed",
                "message": "NearbyCast sender processes exited",
            });
        }
    }

    let child_exited = {
        let mut cast = state.cast_child.lock().unwrap();
        match cast.as_mut().map(Child::try_wait) {
            Some(Ok(Some(exit_status))) => {
                *cast = None;
                Some(format!("Cast launcher exited with {exit_status}"))
            }
            Some(Err(error)) => Some(format!("Could not inspect cast launcher: {error}")),
            Some(Ok(None)) => None,
            None => {
                if state.cast_spec.lock().unwrap().is_some()
                    && !*state.stop_requested.lock().unwrap()
                {
                    Some("No active cast launcher".to_string())
                } else {
                    None
                }
            }
        }
    };

    if let Some(error) = child_exited {
        *state.cast_protocol.lock().unwrap() = None;
        if let Some(status) = reported_status.clone() {
            if status.get("state").and_then(serde_json::Value::as_str) == Some("failed") {
                // Intentional protocol failure (auth/media reject) — do not loop forever.
                if status
                    .get("message")
                    .and_then(|value| value.as_str())
                    .is_some_and(|message| {
                        message.to_ascii_lowercase().contains("reject")
                            || message.to_ascii_lowercase().contains("not found")
                    })
                {
                    return status;
                }
            }
        }
        if let Some(status) = schedule_reconnect(&state, &error) {
            return status;
        }
        return reported_status
            .unwrap_or_else(|| serde_json::json!({ "state": "failed", "message": error }));
    }

    if let Some(status) = reported_status {
        if status.get("state").and_then(serde_json::Value::as_str) == Some("casting") {
            state.reconnect.lock().unwrap().mark_recovered();
        }
        return status;
    }
    serde_json::json!({
        "state": "starting",
        "message": "Waiting for the cast launcher to report capture status",
    })
}

/// Start the authenticated NearbyCast receiver control plane.
#[tauri::command]
fn start_receiver(listen_port: u16, state: State<AppState>) -> serde_json::Value {
    {
        let mut existing = state.nearby_receiver.lock().unwrap();
        if let Some(receiver) = existing.as_mut() {
            receiver.stop();
        }
        *existing = None;
    }
    match nearby_protocol::NearbyReceiver::start(listen_port, nearby_protocol::config_dir()) {
        Ok(receiver) => {
            let port = receiver.control_port;
            *state.nearby_receiver.lock().unwrap() = Some(receiver);
            serde_json::json!({
                "ok": true,
                "port": port,
                "message": "NearbyCast receiver is listening for authenticated sessions",
            })
        }
        Err(error) => serde_json::json!({ "ok": false, "error": error }),
    }
}

/// Stop receiver
#[tauri::command]
fn stop_receiver(state: State<AppState>) -> bool {
    {
        let mut receiver = state.nearby_receiver.lock().unwrap();
        if let Some(active) = receiver.as_mut() {
            active.stop();
        }
        *receiver = None;
    }
    let mut recv = state.recv_child.lock().unwrap();
    terminate_cast_child(&mut recv);
    true
}

#[tauri::command]
fn nearby_pairing_code(state: State<AppState>) -> serde_json::Value {
    let code = state
        .nearby_receiver
        .lock()
        .unwrap()
        .as_ref()
        .and_then(nearby_protocol::NearbyReceiver::current_pairing_code);
    serde_json::json!({ "code": code })
}

/// Get local IP address that can reach the given target IP
#[tauri::command]
fn get_local_ip(target_ip: String) -> Option<String> {
    let target_ip = parse_routable_ip(&target_ip).ok()?.to_string();
    // Connect UDP socket (no data sent) to discover outgoing interface IP
    let target = format!("{}:80", target_ip);
    if let Ok(socket) = UdpSocket::bind("0.0.0.0:0") {
        if socket.connect(&target as &str).is_ok() {
            if let Ok(addr) = socket.local_addr() {
                return Some(addr.ip().to_string());
            }
        }
    }
    // Fallback: parse `ip route` output
    if let Ok(out) = Command::new("ip")
        .args(["route", "get", &target_ip])
        .output()
    {
        let s = String::from_utf8_lossy(&out.stdout).to_string();
        for part in s.split_whitespace() {
            if let Ok(ip) = part.parse::<std::net::IpAddr>() {
                if !ip.is_loopback() {
                    return Some(ip.to_string());
                }
            }
        }
    }
    None
}
#[tauri::command]
fn check_dependencies() -> serde_json::Value {
    // Required for the primary Google Cast screen-cast path.
    let required = ["wf-recorder", "ffmpeg", "python3"];
    // Optional helpers: Miracast/WFD, region pick, local NearbyCast playback, Wi-Fi P2P scan.
    let optional = ["mpv", "wlr-randr", "slurp", "nmcli", "gdbus", "fluxcast"];

    let tool_present = |tool: &str| -> bool {
        if tool == "fluxcast" {
            return receivers::miracast::fluxcast_available();
        }
        Command::new("which")
            .arg(tool)
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
    };

    let mut required_map = serde_json::Map::new();
    for tool in required {
        required_map.insert(
            tool.to_string(),
            serde_json::Value::Bool(tool_present(tool)),
        );
    }
    let py_cast = Command::new("python3")
        .args(["-c", "import pychromecast"])
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    required_map.insert("pychromecast".to_string(), serde_json::Value::Bool(py_cast));

    let mut optional_map = serde_json::Map::new();
    for tool in optional {
        optional_map.insert(
            tool.to_string(),
            serde_json::Value::Bool(tool_present(tool)),
        );
    }

    // Flat keys kept for older UI callers; required tools also appear at top level.
    let mut flat = serde_json::Map::new();
    for (k, v) in required_map.iter() {
        flat.insert(k.clone(), v.clone());
    }
    for (k, v) in optional_map.iter() {
        flat.insert(k.clone(), v.clone());
    }
    flat.insert(
        "required".to_string(),
        serde_json::Value::Object(required_map),
    );
    flat.insert(
        "optional".to_string(),
        serde_json::Value::Object(optional_map),
    );
    serde_json::Value::Object(flat)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let discovery_service = Arc::new(Mutex::new(DiscoveryService::new()));

    let state = AppState {
        discovery: discovery_service,
        cast_child: Mutex::new(None),
        cast_session_dir: Mutex::new(None),
        cast_protocol: Mutex::new(None),
        recv_child: Mutex::new(None),
        nearby_receiver: Mutex::new(None),
        nearby_sender: Mutex::new(None),
        cast_spec: Mutex::new(None),
        reconnect: Mutex::new(reconnect::ReconnectPolicy::default()),
        stop_requested: Mutex::new(false),
    };

    tauri::Builder::default()
        .setup(|app| {
            #[cfg(target_os = "linux")]
            {
                use tauri::Manager;
                use webkit2gtk::{PermissionRequestExt, SettingsExt, WebViewExt};
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.with_webview(|webview| {
                        let wv = webview.inner();
                        if let Some(settings) = wv.settings() {
                            settings.set_enable_media_stream(true);
                        }
                        wv.connect_permission_request(|_, req| {
                            req.allow();
                            true
                        });
                    });
                }
            }
            Ok(())
        })
        .manage(state)
        .invoke_handler(tauri::generate_handler![
            get_discovered_devices,
            scan_miracast_devices,
            list_outputs,
            list_windows,
            select_region_slurp,
            start_projection,
            stop_projection,
            attempt_reconnect,
            start_receiver,
            stop_receiver,
            nearby_pairing_code,
            check_dependencies,
            check_cast_alive,
            diagnose_device,
            get_local_ip,
        ])
        .on_window_event(|window, event| {
            // Auto-stop projection when the main window is closed
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                use tauri::Manager;
                let app = window.app_handle();
                if let Some(state) = app.try_state::<AppState>() {
                    let mut cast = state.cast_child.lock().unwrap();
                    terminate_cast_child(&mut cast);
                    drop(cast);
                    {
                        let mut sender = state.nearby_sender.lock().unwrap();
                        if let Some(session) = sender.as_mut() {
                            session.stop();
                        }
                        *sender = None;
                    }
                    {
                        let mut receiver = state.nearby_receiver.lock().unwrap();
                        if let Some(active) = receiver.as_mut() {
                            active.stop();
                        }
                        *receiver = None;
                    }
                    clear_cast_session_dir(&state);
                    *state.cast_protocol.lock().unwrap() = None;
                    let mut recv = state.recv_child.lock().unwrap();
                    terminate_cast_child(&mut recv);
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Nearby Cast");
}

#[cfg(test)]
mod tests {
    use super::{parse_fluxcast_wfd_scan, parse_routable_ip};

    #[test]
    fn accepts_a_routable_ipv4_receiver() {
        assert_eq!(
            parse_routable_ip("192.168.1.20").unwrap().to_string(),
            "192.168.1.20"
        );
    }

    #[test]
    fn rejects_non_routable_or_unsupported_receiver_addresses() {
        for value in ["127.0.0.1", "0.0.0.0", "224.0.0.1", "not-an-ip", "fe80::1"] {
            assert!(
                parse_routable_ip(value).is_err(),
                "{value} should be rejected"
            );
        }
    }

    #[test]
    fn rejects_injection_shaped_receiver_addresses() {
        for value in [
            "192.168.1.1;rm -rf /",
            "192.168.1.1\n",
            "$(reboot)",
            "`id`",
            "192.168.1.1$(reboot)",
            "",
        ] {
            assert!(
                parse_routable_ip(value).is_err(),
                "{value:?} should be rejected"
            );
        }
    }

    #[test]
    fn only_exposes_wfd_peers_with_capability_evidence() {
        let devices = parse_fluxcast_wfd_scan(
            "[FluxCast WFD] Wi-Fi Direct peer(s):\n  [0] AA:BB:CC:DD:EE:FF  Living Room via NetworkManager\n      WFD capability data detected\n  [1] 11:22:33:44:55:66  ordinary peer via NetworkManager\n",
        );
        assert_eq!(devices.len(), 1);
        assert_eq!(devices[0].ip_address, "aa:bb:cc:dd:ee:ff");
        assert_eq!(devices[0].name, "Living Room");
    }
}
