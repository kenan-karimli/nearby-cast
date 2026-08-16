//! NearbyCast native protocol: authenticated control plane + session-owned media.
//!
//! A receiver never accepts an anonymous media stream. Media TCP connections must
//! present a short-lived session token issued only after pairing or prior trust.
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::identity::{DeviceIdentity, IdentityManager};

const TRUST_FILE: &str = "trusted_devices.json";
const DEFAULT_CONTROL_PORT: u16 = 29870;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ControlMessage {
    Hello {
        device_id: String,
        public_key_hex: String,
    },
    PairRequired {
        pairing_id: String,
        code_hint: String,
        expires_in_secs: u64,
    },
    PairSubmit {
        pairing_id: String,
        code: String,
    },
    Authorized {
        session_token: String,
        media_port: u16,
    },
    Reject {
        reason: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct TrustedDevice {
    device_id: String,
    public_key_hex: String,
    trusted_at: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct TrustStoreFile {
    devices: Vec<TrustedDevice>,
}

#[derive(Debug)]
struct PendingPairing {
    device_id: String,
    public_key_hex: String,
    code: String,
    expires_at: Instant,
}

pub struct NearbyReceiver {
    stop: Arc<AtomicBool>,
    join: Option<thread::JoinHandle<()>>,
    mdns: Option<mdns_sd::ServiceDaemon>,
    mdns_fullname: Option<String>,
    pub control_port: u16,
    pub pairing_code: Arc<Mutex<Option<String>>>,
}

impl NearbyReceiver {
    pub fn start(control_port: u16, config_dir: PathBuf) -> Result<Self, String> {
        let port = if control_port == 0 {
            DEFAULT_CONTROL_PORT
        } else {
            control_port
        };
        let listener = TcpListener::bind(("0.0.0.0", port))
            .map_err(|error| format!("Could not bind NearbyCast receiver on {port}: {error}"))?;

        let identity = IdentityManager::new(config_dir.clone()).get_or_create_identity()?;
        let stop = Arc::new(AtomicBool::new(false));
        let pairing_code = Arc::new(Mutex::new(None));
        let stop_flag = Arc::clone(&stop);
        let pairing_code_thread = Arc::clone(&pairing_code);

        let (mdns, mdns_fullname) = advertise_receiver(&identity, port)?;

        let join = thread::spawn(move || {
            run_receiver_loop(listener, config_dir, stop_flag, pairing_code_thread);
        });

        Ok(Self {
            stop,
            join: Some(join),
            mdns,
            mdns_fullname,
            control_port: port,
            pairing_code,
        })
    }

    pub fn current_pairing_code(&self) -> Option<String> {
        self.pairing_code.lock().unwrap().clone()
    }

    pub fn stop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        let _ = TcpStream::connect(("127.0.0.1", self.control_port));
        if let Some(fullname) = self.mdns_fullname.take() {
            if let Some(mdns) = self.mdns.as_ref() {
                let _ = mdns.unregister(&fullname);
            }
        }
        if let Some(mdns) = self.mdns.take() {
            let _ = mdns.shutdown();
        }
        if let Some(handle) = self.join.take() {
            let _ = handle.join();
        }
        *self.pairing_code.lock().unwrap() = None;
    }
}

impl Drop for NearbyReceiver {
    fn drop(&mut self) {
        self.stop();
    }
}

fn advertise_receiver(
    identity: &DeviceIdentity,
    port: u16,
) -> Result<(Option<mdns_sd::ServiceDaemon>, Option<String>), String> {
    let Ok(mdns) = mdns_sd::ServiceDaemon::new() else {
        return Ok((None, None));
    };
    let host_ip = local_ip_address::local_ip()
        .map(|ip| ip.to_string())
        .unwrap_or_else(|_| "127.0.0.1".into());
    let instance = identity.device_id.replace('_', "-");
    let host_name = format!("{instance}.local.");
    let properties = [
        ("id", identity.device_id.as_str()),
        ("pk", identity.public_key_hex.as_str()),
        ("proto", "nearbycast"),
        ("ver", "1"),
    ];
    let service = mdns_sd::ServiceInfo::new(
        "_nearbycast._tcp.local.",
        &instance,
        &host_name,
        host_ip.as_str(),
        port,
        &properties[..],
    )
    .map_err(|e| format!("Could not build NearbyCast mDNS record: {e}"))?;
    let fullname = service.get_fullname().to_string();
    mdns.register(service)
        .map_err(|e| format!("Could not advertise NearbyCast receiver: {e}"))?;
    Ok((Some(mdns), Some(fullname)))
}

fn run_receiver_loop(
    listener: TcpListener,
    config_dir: PathBuf,
    stop: Arc<AtomicBool>,
    pairing_code: Arc<Mutex<Option<String>>>,
) {
    let pending: Arc<Mutex<HashMap<String, PendingPairing>>> = Arc::new(Mutex::new(HashMap::new()));
    let active_tokens: Arc<Mutex<HashMap<String, Instant>>> = Arc::new(Mutex::new(HashMap::new()));

    listener.set_nonblocking(true).ok();
    while !stop.load(Ordering::SeqCst) {
        match listener.accept() {
            Ok((stream, _)) => {
                let config_dir = config_dir.clone();
                let pending = Arc::clone(&pending);
                let active_tokens = Arc::clone(&active_tokens);
                let pairing_code = Arc::clone(&pairing_code);
                thread::spawn(move || {
                    let _ = handle_control_connection(
                        stream,
                        &config_dir,
                        pending,
                        active_tokens,
                        pairing_code,
                    );
                });
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(100));
            }
            Err(_) => thread::sleep(Duration::from_millis(100)),
        }
    }
}

fn handle_control_connection(
    mut stream: TcpStream,
    config_dir: &Path,
    pending: Arc<Mutex<HashMap<String, PendingPairing>>>,
    active_tokens: Arc<Mutex<HashMap<String, Instant>>>,
    pairing_code: Arc<Mutex<Option<String>>>,
) -> Result<(), String> {
    stream.set_read_timeout(Some(Duration::from_secs(30))).ok();
    let mut reader = BufReader::new(stream.try_clone().map_err(|e| e.to_string())?);
    let mut line = String::new();
    reader
        .read_line(&mut line)
        .map_err(|e| format!("Failed to read NearbyCast hello: {e}"))?;
    let hello: ControlMessage =
        serde_json::from_str(line.trim()).map_err(|e| format!("Invalid NearbyCast hello: {e}"))?;

    let (device_id, public_key_hex) = match hello {
        ControlMessage::Hello {
            device_id,
            public_key_hex,
        } => (device_id, public_key_hex),
        _ => {
            write_message(
                &mut stream,
                &ControlMessage::Reject {
                    reason: "Expected hello".into(),
                },
            )?;
            return Ok(());
        }
    };

    if public_key_hex.len() != 64 || decode_hex_len(&public_key_hex) != Some(32) {
        write_message(
            &mut stream,
            &ControlMessage::Reject {
                reason: "Invalid public key".into(),
            },
        )?;
        return Ok(());
    }

    if is_trusted(config_dir, &device_id, &public_key_hex)? {
        return authorize_and_serve_media(stream, active_tokens);
    }

    let pairing_id = format!("{:032x}", rand::random::<u128>());
    let code_value: u32 = rand::random::<u32>() % 900_000 + 100_000;
    let code = format!("{code_value:06}");
    let display_code = format!("{} {}", &code[0..3], &code[3..6]);
    pending.lock().unwrap().insert(
        pairing_id.clone(),
        PendingPairing {
            device_id: device_id.clone(),
            public_key_hex: public_key_hex.clone(),
            code: display_code.clone(),
            expires_at: Instant::now() + Duration::from_secs(120),
        },
    );
    *pairing_code.lock().unwrap() = Some(display_code.clone());

    write_message(
        &mut stream,
        &ControlMessage::PairRequired {
            pairing_id: pairing_id.clone(),
            code_hint: "Confirm the six-digit code shown on the receiver".into(),
            expires_in_secs: 120,
        },
    )?;

    line.clear();
    reader
        .read_line(&mut line)
        .map_err(|e| format!("Failed to read pairing submit: {e}"))?;
    let submit: ControlMessage =
        serde_json::from_str(line.trim()).map_err(|e| format!("Invalid pairing submit: {e}"))?;

    let (submit_id, submit_code) = match submit {
        ControlMessage::PairSubmit { pairing_id, code } => (pairing_id, code),
        _ => {
            write_message(
                &mut stream,
                &ControlMessage::Reject {
                    reason: "Expected pair_submit".into(),
                },
            )?;
            return Ok(());
        }
    };

    let accepted = {
        let mut map = pending.lock().unwrap();
        match map.remove(&submit_id) {
            Some(entry)
                if entry.device_id == device_id
                    && entry.expires_at > Instant::now()
                    && normalize_code(&entry.code) == normalize_code(&submit_code) =>
            {
                trust_device(config_dir, &entry.device_id, &entry.public_key_hex)?;
                *pairing_code.lock().unwrap() = None;
                true
            }
            _ => false,
        }
    };

    if !accepted {
        write_message(
            &mut stream,
            &ControlMessage::Reject {
                reason: "Pairing code rejected or expired".into(),
            },
        )?;
        return Ok(());
    }

    authorize_and_serve_media(stream, active_tokens)
}

fn authorize_and_serve_media(
    mut stream: TcpStream,
    active_tokens: Arc<Mutex<HashMap<String, Instant>>>,
) -> Result<(), String> {
    let media_listener = TcpListener::bind(("0.0.0.0", 0))
        .map_err(|e| format!("Could not bind NearbyCast media port: {e}"))?;
    let media_port = media_listener
        .local_addr()
        .map_err(|e| format!("Could not read media port: {e}"))?
        .port();
    let session_token = format!("{:032x}", rand::random::<u128>());
    active_tokens.lock().unwrap().insert(
        session_token.clone(),
        Instant::now() + Duration::from_secs(30),
    );

    write_message(
        &mut stream,
        &ControlMessage::Authorized {
            session_token: session_token.clone(),
            media_port,
        },
    )?;

    media_listener
        .set_nonblocking(false)
        .map_err(|e| e.to_string())?;
    let (mut media_stream, _) = media_listener
        .accept()
        .map_err(|e| format!("NearbyCast media connection failed: {e}"))?;

    let mut header = String::new();
    let mut reader = BufReader::new(media_stream.try_clone().map_err(|e| e.to_string())?);
    reader
        .read_line(&mut header)
        .map_err(|e| format!("Failed to read media auth header: {e}"))?;
    let presented = header
        .trim()
        .strip_prefix("NEARBYCAST-SESSION ")
        .unwrap_or("");
    let allowed = {
        let mut tokens = active_tokens.lock().unwrap();
        matches!(tokens.remove(presented), Some(expires) if expires > Instant::now() && presented == session_token)
    };
    if !allowed {
        return Err("Unauthorized NearbyCast media connection".into());
    }

    let mut player = Command::new("mpv")
        .args([
            "--no-terminal",
            "--force-window=yes",
            "--title=NearbyCast Receiver",
            "--demuxer-lavf-format=mpegts",
            "-",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("Could not start NearbyCast media player: {e}"))?;

    if let Some(mut stdin) = player.stdin.take() {
        let mut buffer = [0u8; 64 * 1024];
        loop {
            match media_stream.read(&mut buffer) {
                Ok(0) => break,
                Ok(n) => {
                    if stdin.write_all(&buffer[..n]).is_err() {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
    }
    let _ = player.kill();
    let _ = player.wait();
    Ok(())
}

/// Parameters for an authenticated NearbyCast sender session.
pub struct NearbySenderRequest<'a> {
    pub target_ip: &'a str,
    pub control_port: u16,
    pub output_name: &'a str,
    pub audio_mode: &'a str,
    pub local_identity: &'a DeviceIdentity,
    pub session_dir: &'a Path,
    pub pairing_code: Option<&'a str>,
    pub video_width: u32,
    pub video_height: u32,
}

/// Owns the authenticated sender capture/encode children for one session.
pub struct NearbySenderSession {
    pub children: Vec<Child>,
}

impl NearbySenderSession {
    pub fn start(request: NearbySenderRequest<'_>) -> Result<Self, String> {
        let NearbySenderRequest {
            target_ip,
            control_port,
            output_name,
            audio_mode,
            local_identity,
            session_dir,
            pairing_code,
            video_width,
            video_height,
        } = request;
        let video_width = video_width.clamp(16, 7680);
        let video_height = video_height.clamp(16, 4320);
        let size = format!("{video_width}x{video_height}");
        let lab_media = std::env::var("NEARBY_CAST_LAB_MEDIA")
            .ok()
            .map(|value| matches!(value.to_ascii_lowercase().as_str(), "1" | "true" | "yes"))
            .unwrap_or(false);
        let mut stream = TcpStream::connect((target_ip, control_port))
            .map_err(|e| format!("Could not reach NearbyCast receiver: {e}"))?;
        stream.set_read_timeout(Some(Duration::from_secs(120))).ok();
        write_message(
            &mut stream,
            &ControlMessage::Hello {
                device_id: local_identity.device_id.clone(),
                public_key_hex: local_identity.public_key_hex.clone(),
            },
        )?;

        let response = read_message(&mut stream)?;
        let (session_token, media_port) = match response {
            ControlMessage::Authorized {
                session_token,
                media_port,
            } => (session_token, media_port),
            ControlMessage::PairRequired { pairing_id, .. } => {
                let code = pairing_code.ok_or_else(|| {
                    "This NearbyCast receiver requires pairing. Enter the code shown on the receiver, then reconnect.".to_string()
                })?;
                write_message(
                    &mut stream,
                    &ControlMessage::PairSubmit {
                        pairing_id,
                        code: code.to_string(),
                    },
                )?;
                match read_message(&mut stream)? {
                    ControlMessage::Authorized {
                        session_token,
                        media_port,
                    } => (session_token, media_port),
                    ControlMessage::Reject { reason } => return Err(reason),
                    _ => return Err("Unexpected NearbyCast pairing response".into()),
                }
            }
            ControlMessage::Reject { reason } => return Err(reason),
            _ => return Err("Unexpected NearbyCast control response".into()),
        };

        let mut media = TcpStream::connect((target_ip, media_port))
            .map_err(|e| format!("Could not open NearbyCast media channel: {e}"))?;
        writeln!(media, "NEARBYCAST-SESSION {session_token}")
            .map_err(|e| format!("Failed to authenticate media channel: {e}"))?;

        let relay = TcpListener::bind("127.0.0.1:0")
            .map_err(|e| format!("Could not bind local media relay: {e}"))?;
        let relay_port = relay.local_addr().map_err(|e| e.to_string())?.port();
        thread::spawn(move || {
            if let Ok((mut inbound, _)) = relay.accept() {
                let mut buffer = [0u8; 64 * 1024];
                loop {
                    match inbound.read(&mut buffer) {
                        Ok(0) => break,
                        Ok(n) => {
                            if media.write_all(&buffer[..n]).is_err() {
                                break;
                            }
                        }
                        Err(_) => break,
                    }
                }
            }
        });

        let silent = audio_mode == "silent";
        let mut children = Vec::new();

        if lab_media {
            // Deterministic MPEG-TS without requiring a Wayland capture path.
            // Used by the virtual receiver lab / CI; production still uses
            // wf-recorder when NEARBY_CAST_LAB_MEDIA is unset.
            let mut ff = Command::new("ffmpeg");
            ff.args([
                "-y",
                "-f",
                "lavfi",
                "-i",
                &format!("testsrc=size={size}:rate=30"),
            ]);
            if !silent {
                ff.args(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]);
            }
            ff.args([
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "30",
                "-bf",
                "0",
            ]);
            if silent {
                ff.arg("-an");
            } else {
                ff.args(["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-shortest"]);
            }
            ff.args(["-f", "mpegts", &format!("tcp://127.0.0.1:{relay_port}")])
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            let encoder = ff
                .spawn()
                .map_err(|e| format!("Could not start NearbyCast lab encoder: {e}"))?;
            children.push(encoder);
        } else {
            let pipe_path = session_dir.join("nearby.raw");
            let _ = fs::remove_file(&pipe_path);
            create_fifo(&pipe_path)?;

            let mut wfr = Command::new("wf-recorder");
            wfr.arg("-o")
                .arg(output_name)
                .args([
                    "-r",
                    "30",
                    "--no-damage",
                    "--muxer=rawvideo",
                    "-c",
                    "rawvideo",
                    "-x",
                    "bgr0",
                    "-f",
                ])
                .arg(&pipe_path)
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            let capture = wfr
                .spawn()
                .map_err(|e| format!("Could not start screen capture for NearbyCast: {e}"))?;

            let mut ff = Command::new("ffmpeg");
            ff.arg("-y").args([
                "-f",
                "rawvideo",
                "-pix_fmt",
                "bgr0",
                "-video_size",
                &size,
                "-framerate",
                "30",
                "-i",
            ]);
            ff.arg(&pipe_path);
            if !silent {
                ff.args(["-f", "pulse", "-i", "default.monitor"]);
            }
            ff.args([
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "30",
                "-bf",
                "0",
                "-vf",
                &format!(
                    "scale={video_width}:{video_height}:force_original_aspect_ratio=decrease,pad={video_width}:{video_height}:(ow-iw)/2:(oh-ih)/2"
                ),
            ]);
            if silent {
                ff.arg("-an");
            } else {
                ff.args(["-c:a", "aac", "-b:a", "128k", "-ar", "44100"]);
            }
            ff.args(["-f", "mpegts", &format!("tcp://127.0.0.1:{relay_port}")])
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            let encoder = ff
                .spawn()
                .map_err(|e| format!("Could not start NearbyCast encoder: {e}"))?;
            children.push(capture);
            children.push(encoder);
        }

        Ok(Self { children })
    }

    pub fn stop(&mut self) {
        for child in &mut self.children {
            let _ = Command::new("kill")
                .args(["-INT", &child.id().to_string()])
                .status();
            let _ = child.wait();
        }
        self.children.clear();
    }
}

impl Drop for NearbySenderSession {
    fn drop(&mut self) {
        self.stop();
    }
}

fn write_message(stream: &mut TcpStream, message: &ControlMessage) -> Result<(), String> {
    let mut payload = serde_json::to_string(message)
        .map_err(|e| format!("Failed to encode control message: {e}"))?;
    payload.push('\n');
    stream
        .write_all(payload.as_bytes())
        .map_err(|e| format!("Failed to send control message: {e}"))
}

fn read_message(stream: &mut TcpStream) -> Result<ControlMessage, String> {
    let mut reader = BufReader::new(stream.try_clone().map_err(|e| e.to_string())?);
    let mut line = String::new();
    reader
        .read_line(&mut line)
        .map_err(|e| format!("Failed to read control message: {e}"))?;
    serde_json::from_str(line.trim()).map_err(|e| format!("Invalid control message: {e}"))
}

fn normalize_code(value: &str) -> String {
    value.chars().filter(|c| c.is_ascii_digit()).collect()
}

fn decode_hex_len(value: &str) -> Option<usize> {
    if !value.len().is_multiple_of(2) {
        return None;
    }
    (0..value.len())
        .step_by(2)
        .map(|index| u8::from_str_radix(&value[index..index + 2], 16).ok())
        .collect::<Option<Vec<_>>>()
        .map(|bytes| bytes.len())
}

fn trust_path(config_dir: &Path) -> PathBuf {
    config_dir.join(TRUST_FILE)
}

fn load_trust(config_dir: &Path) -> Result<TrustStoreFile, String> {
    let path = trust_path(config_dir);
    if !path.exists() {
        return Ok(TrustStoreFile::default());
    }
    let content =
        fs::read_to_string(&path).map_err(|e| format!("Failed to read trust store: {e}"))?;
    serde_json::from_str(&content).map_err(|e| format!("Failed to parse trust store: {e}"))
}

fn save_trust(config_dir: &Path, store: &TrustStoreFile) -> Result<(), String> {
    fs::create_dir_all(config_dir).map_err(|e| format!("Failed to create config dir: {e}"))?;
    let path = trust_path(config_dir);
    let content = serde_json::to_string_pretty(store)
        .map_err(|e| format!("Failed to serialize trust store: {e}"))?;
    let temporary = path.with_extension("json.tmp");
    fs::write(&temporary, content).map_err(|e| format!("Failed to write trust store: {e}"))?;
    fs::rename(&temporary, &path).map_err(|e| format!("Failed to publish trust store: {e}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
            .map_err(|e| format!("Failed to protect trust store: {e}"))?;
    }
    Ok(())
}

pub fn is_trusted(
    config_dir: &Path,
    device_id: &str,
    public_key_hex: &str,
) -> Result<bool, String> {
    let store = load_trust(config_dir)?;
    Ok(store
        .devices
        .iter()
        .any(|device| device.device_id == device_id && device.public_key_hex == public_key_hex))
}

pub fn trust_device(
    config_dir: &Path,
    device_id: &str,
    public_key_hex: &str,
) -> Result<(), String> {
    let mut store = load_trust(config_dir)?;
    store.devices.retain(|device| device.device_id != device_id);
    store.devices.push(TrustedDevice {
        device_id: device_id.to_string(),
        public_key_hex: public_key_hex.to_string(),
        trusted_at: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs(),
    });
    save_trust(config_dir, &store)
}

pub fn revoke_device(config_dir: &Path, device_id: &str) -> Result<(), String> {
    let mut store = load_trust(config_dir)?;
    store.devices.retain(|device| device.device_id != device_id);
    save_trust(config_dir, &store)
}

fn create_fifo(path: &Path) -> Result<(), String> {
    let status = Command::new("mkfifo")
        .arg(path)
        .status()
        .map_err(|e| format!("Could not create capture FIFO: {e}"))?;
    if !status.success() {
        return Err(format!("mkfifo failed for {}", path.display()));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .map_err(|e| format!("Failed to protect capture FIFO: {e}"))?;
    }
    Ok(())
}

pub fn config_dir() -> PathBuf {
    dirs_fallback()
}

fn dirs_fallback() -> PathBuf {
    if let Ok(xdg) = std::env::var("XDG_CONFIG_HOME") {
        return PathBuf::from(xdg).join("nearby-cast");
    }
    if let Ok(home) = std::env::var("HOME") {
        return PathBuf::from(home).join(".config/nearby-cast");
    }
    std::env::temp_dir().join("nearby-cast-config")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn trust_store_round_trip_requires_matching_key() {
        let dir = std::env::temp_dir().join(format!("nearby-trust-{}", rand::random::<u64>()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        trust_device(&dir, "nc-abc", &"aa".repeat(32)).unwrap();
        assert!(is_trusted(&dir, "nc-abc", &"aa".repeat(32)).unwrap());
        assert!(!is_trusted(&dir, "nc-abc", &"bb".repeat(32)).unwrap());
        revoke_device(&dir, "nc-abc").unwrap();
        assert!(!is_trusted(&dir, "nc-abc", &"aa".repeat(32)).unwrap());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn control_messages_serialize_with_type_tag() {
        let message = ControlMessage::Hello {
            device_id: "nc-1".into(),
            public_key_hex: "ab".repeat(32),
        };
        let encoded = serde_json::to_string(&message).unwrap();
        assert!(encoded.contains("\"type\":\"hello\""));
        let decoded: ControlMessage = serde_json::from_str(&encoded).unwrap();
        assert_eq!(decoded, message);
    }

    #[test]
    fn untrusted_hello_requires_pairing_before_media() {
        let dir = std::env::temp_dir().join(format!("nearby-recv-{}", rand::random::<u64>()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let mut receiver = NearbyReceiver::start(port, dir.clone()).unwrap();
        thread::sleep(Duration::from_millis(50));

        let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        write_message(
            &mut stream,
            &ControlMessage::Hello {
                device_id: "nc-test".into(),
                public_key_hex: "cd".repeat(32),
            },
        )
        .unwrap();
        let response = read_message(&mut stream).unwrap();
        match response {
            ControlMessage::PairRequired { .. } => {}
            other => panic!("expected pair_required, got {other:?}"),
        }
        assert!(receiver.current_pairing_code().is_some());
        receiver.stop();
        let _ = fs::remove_dir_all(&dir);
    }
}
