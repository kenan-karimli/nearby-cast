use mdns_sd::{ServiceDaemon, ServiceEvent};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::net::UdpSocket;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const DEVICE_TTL: Duration = Duration::from_secs(90);
const SSDP_REFRESH: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum DetectedProtocol {
    NearbyCast,
    GoogleCast,
    AirPlay,
    Miracast,
    Unknown,
}

impl std::fmt::Display for DetectedProtocol {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NearbyCast => write!(f, "Nearby Cast"),
            Self::GoogleCast => write!(f, "Google Cast"),
            Self::AirPlay => write!(f, "AirPlay"),
            Self::Miracast => write!(f, "Miracast"),
            Self::Unknown => write!(f, "Unknown"),
        }
    }
}

/// Capability flags are evidence-based: discovery only supplies advertisement
/// evidence, while `screen_cast` is true only for a local sender path that
/// exists in this product build.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeviceCapabilities {
    pub screen_cast: bool,
    pub media_cast: bool,
    pub audio_cast: bool,
    pub requires_pin: bool,
    pub support_notes: String,
    /// advertised | implemented | simulator_verified | physical_verified
    pub verification: String,
}

impl DeviceCapabilities {
    fn for_google_cast() -> Self {
        Self {
            screen_cast: true,
            media_cast: true,
            audio_cast: true,
            requires_pin: false,
              support_notes: "Advertised Google Cast receiver; NearbyCast uses fragmented MP4 by default and verifies PLAYING before reporting Connected.".into(),
            verification: "implemented".into(),
        }
    }

    fn for_nearby_cast() -> Self {
        Self {
            screen_cast: true,
            media_cast: false,
            audio_cast: true,
            requires_pin: true,
            support_notes: "NearbyCast authenticated sender/receiver is available. Pairing is required before the first trusted stream.".into(),
            verification: "simulator_verified".into(),
        }
    }

    fn for_airplay() -> Self {
        Self {
            screen_cast: false,
            media_cast: false,
            audio_cast: false,
            requires_pin: true,
            support_notes:
                "AirPlay service advertised. Modern Apple FairPlay auth is NOT VERIFIED; use an AirPlay lab receiver for simulator-verified mirroring."
                    .into(),
            verification: "advertised".into(),
        }
    }

    fn for_airplay_lab() -> Self {
        Self {
            screen_cast: true,
            media_cast: false,
            audio_cast: true,
            requires_pin: true,
            support_notes: "Virtual AirPlay lab sink (PIN pair + RTSP mirroring). Protocol stack can be verified; physical FairPlay Apple TV remains NOT VERIFIED.".into(),
            verification: "simulator_verified".into(),
        }
    }

    fn for_miracast() -> Self {
        // mDNS `_display._tcp` advertisements are not Wi-Fi Direct evidence.
        // Castable Miracast peers come from the active WFD scan path or the
        // explicit WFD lab RTSP endpoint (`_wfd-lab._tcp`).
        Self {
            screen_cast: false,
            media_cast: false,
            audio_cast: false,
            requires_pin: false,
            support_notes: "Network display service advertised over LAN. Use Wireless scan for Wi-Fi Direct Miracast peers when FluxCast is installed.".into(),
            verification: "advertised".into(),
        }
    }

    fn for_miracast_lab() -> Self {
        Self {
            screen_cast: true,
            media_cast: false,
            audio_cast: true,
            requires_pin: false,
            support_notes: "Virtual WFD RTSP/RTP lab sink. Protocol stack can be verified; physical Wi-Fi Direct P2P remains NOT VERIFIED.".into(),
            verification: "simulator_verified".into(),
        }
    }

    fn unknown() -> Self {
        Self {
            screen_cast: false,
            media_cast: false,
            audio_cast: false,
            requires_pin: false,
            support_notes:
                "Generic network display advertisement; no compatible sender path verified.".into(),
            verification: "advertised".into(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DiscoveredDevice {
    pub id: String,
    pub name: String,
    pub platform: String,
    pub version: String,
    pub capabilities: Vec<String>,
    pub protocol_version: u32,
    pub ip_address: String,
    pub port: u16,
    pub status: String,
    pub is_trusted: bool,
    /// Preferred locally usable protocol. It is not a claim that every
    /// protocol advertised by the endpoint is usable.
    pub protocol: DetectedProtocol,
    /// All protocols discovered at this endpoint, for diagnostics and routing.
    pub protocols: Vec<DetectedProtocol>,
    /// mDNS full names and SSDP identifiers that led to this record.
    pub services: Vec<String>,
    pub last_seen_unix_secs: u64,
    pub device_capabilities: DeviceCapabilities,
    /// True when at least one advertisement marked this endpoint as a lab sink.
    pub lab: bool,
}

#[derive(Debug, Clone)]
pub struct DiscoveryService {
    registry: Arc<Mutex<DeviceRegistry>>,
}

#[derive(Debug, Default)]
struct DeviceRegistry {
    devices: HashMap<String, DiscoveredDevice>,
    service_index: HashMap<String, String>,
}

struct DiscoveryObservation {
    stable_identity: String,
    service_name: String,
    name: String,
    platform: String,
    ip_address: String,
    port: u16,
    protocol: DetectedProtocol,
    /// True when the advertisement explicitly marks a NearbyCast lab endpoint.
    lab: bool,
}

impl Default for DiscoveryService {
    fn default() -> Self {
        Self::new()
    }
}

impl DiscoveryService {
    pub fn new() -> Self {
        let registry = Arc::new(Mutex::new(DeviceRegistry::default()));
        let service = Self {
            registry: registry.clone(),
        };
        service.start_mdns_listener(registry.clone());
        service.start_ssdp_listener(registry);
        service
    }

    fn start_mdns_listener(&self, registry: Arc<Mutex<DeviceRegistry>>) {
        tauri::async_runtime::spawn(async move {
            let mdns = match ServiceDaemon::new() {
                Ok(daemon) => daemon,
                Err(error) => {
                    eprintln!("[DISCOVERY] mDNS daemon init failed: {error}");
                    return;
                }
            };

            let service_types = [
                ("_googlecast._tcp.local.", DetectedProtocol::GoogleCast),
                ("_airplay._tcp.local.", DetectedProtocol::AirPlay),
                ("_airplay-lab._tcp.local.", DetectedProtocol::AirPlay),
                ("_display._tcp.local.", DetectedProtocol::Miracast),
                ("_wfd-lab._tcp.local.", DetectedProtocol::Miracast),
                ("_nearbycast._tcp.local.", DetectedProtocol::NearbyCast),
                ("_touch-able._tcp.local.", DetectedProtocol::AirPlay),
            ];

            for (service_type, protocol) in service_types {
                let Ok(receiver) = mdns.browse(service_type) else {
                    continue;
                };
                let registry = registry.clone();
                tauri::async_runtime::spawn(async move {
                    while let Ok(event) = receiver.recv_async().await {
                        match event {
                            ServiceEvent::ServiceResolved(info) => {
                                let ip_address = info
                                    .get_addresses()
                                    .iter()
                                    .find(|address| address.is_ipv4())
                                    .map(ToString::to_string)
                                    .unwrap_or_default();
                                if ip_address.is_empty() {
                                    continue;
                                }
                                let allow_loopback = std::env::var("NEARBY_CAST_ALLOW_LOOPBACK")
                                    .ok()
                                    .map(|value| {
                                        matches!(
                                            value.to_ascii_lowercase().as_str(),
                                            "1" | "true" | "yes"
                                        )
                                    })
                                    .unwrap_or(false)
                                    || std::env::var("NEARBY_CAST_VIRTUAL_LAB")
                                        .ok()
                                        .map(|value| {
                                            matches!(
                                                value.to_ascii_lowercase().as_str(),
                                                "1" | "true" | "yes"
                                            )
                                        })
                                        .unwrap_or(false);
                                if ip_address.starts_with("127.") && !allow_loopback {
                                    continue;
                                }
                                let name = info
                                    .get_property("fn")
                                    .or_else(|| info.get_property("name"))
                                    .or_else(|| info.get_property("md"))
                                    .map(|property| property.val_str().to_string())
                                    .filter(|value| !value.trim().is_empty())
                                    .unwrap_or_else(|| {
                                        info.get_hostname().trim_end_matches('.').to_string()
                                    });
                                let stable_identity = info
                                    .get_property("id")
                                    .or_else(|| info.get_property("deviceid"))
                                    .or_else(|| info.get_property("pi"))
                                    .map(|property| property.val_str().to_string())
                                    .filter(|value| !value.trim().is_empty())
                                    .unwrap_or_else(|| {
                                        info.get_hostname().trim_end_matches('.').to_string()
                                    });
                                let lab = info
                                    .get_property("lab")
                                    .map(|property| {
                                        matches!(property.val_str().trim(), "1" | "true" | "yes")
                                    })
                                    .unwrap_or_else(|| {
                                        info.get_fullname().contains("_airplay-lab._tcp")
                                            || info.get_fullname().contains("_wfd-lab._tcp")
                                    });
                                println!(
                                    "[DISCOVERY] mDNS {:?}: name={} ip={} port={} lab={}",
                                    protocol,
                                    name,
                                    ip_address,
                                    info.get_port(),
                                    lab
                                );
                                registry.lock().unwrap().upsert(DiscoveryObservation {
                                    stable_identity,
                                    service_name: info.get_fullname().to_string(),
                                    name,
                                    platform: protocol.to_string(),
                                    ip_address,
                                    port: info.get_port(),
                                    protocol: protocol.clone(),
                                    lab,
                                });
                            }
                            ServiceEvent::ServiceRemoved(_, service_name) => {
                                registry.lock().unwrap().remove_service(&service_name);
                            }
                            _ => {}
                        }
                    }
                });
            }
        });
    }

    fn start_ssdp_listener(&self, registry: Arc<Mutex<DeviceRegistry>>) {
        std::thread::spawn(move || loop {
            let message = "M-SEARCH * HTTP/1.1\r\n\
                 HOST: 239.255.255.250:1900\r\n\
                 MAN: \"ssdp:discover\"\r\n\
                 MX: 3\r\n\
                 ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n";
            if let Ok(socket) = UdpSocket::bind("0.0.0.0:0") {
                let _ = socket.set_read_timeout(Some(Duration::from_secs(4)));
                let _ = socket.send_to(message.as_bytes(), "239.255.255.250:1900");
                let mut buffer = [0u8; 2048];
                for _ in 0..4 {
                    let Ok((length, source)) = socket.recv_from(&mut buffer) else {
                        continue;
                    };
                    let ip_address = source.ip().to_string();
                    if ip_address.starts_with("127.") {
                        continue;
                    }
                    let response = String::from_utf8_lossy(&buffer[..length]);
                    let name = ssdp_name(&response);
                    let service_name = response
                        .lines()
                        .find_map(|line| {
                            line.strip_prefix("USN:")
                                .or_else(|| line.strip_prefix("usn:"))
                        })
                        .map(str::trim)
                        .filter(|value| !value.is_empty())
                        .map(ToOwned::to_owned)
                        .unwrap_or_else(|| format!("ssdp-{ip_address}"));
                    println!("[DISCOVERY] SSDP: name={name} ip={ip_address}");
                    registry.lock().unwrap().upsert(DiscoveryObservation {
                        stable_identity: service_name.clone(),
                        service_name,
                        name,
                        platform: "Smart TV (DLNA/UPnP)".into(),
                        ip_address,
                        port: 1900,
                        protocol: DetectedProtocol::Unknown,
                        lab: false,
                    });
                }
            }
            std::thread::sleep(SSDP_REFRESH);
        });
    }

    pub fn list_devices(&self) -> Vec<DiscoveredDevice> {
        let mut registry = self.registry.lock().unwrap();
        registry.prune_expired();
        let mut devices: Vec<_> = registry.devices.values().cloned().collect();
        devices.sort_by_key(|device| device.name.to_lowercase());
        devices
    }
}

impl DeviceRegistry {
    fn upsert(&mut self, observation: DiscoveryObservation) {
        let identity_key = format!("device:{}", observation.stable_identity.to_lowercase());
        let device_id = self
            .service_index
            .get(&observation.service_name)
            .cloned()
            .or_else(|| {
                self.devices
                    .contains_key(&identity_key)
                    .then_some(identity_key.clone())
            })
            .or_else(|| {
                self.devices
                    .iter()
                    .find(|(_, device)| device.ip_address == observation.ip_address)
                    .map(|(id, _)| id.clone())
            })
            .unwrap_or(identity_key);
        let now = unix_seconds();
        let device = self
            .devices
            .entry(device_id.clone())
            .or_insert_with(|| DiscoveredDevice {
                id: device_id.clone(),
                name: observation.name.clone(),
                platform: observation.platform.clone(),
                version: "1.0".into(),
                capabilities: Vec::new(),
                protocol_version: 1,
                ip_address: observation.ip_address.clone(),
                port: observation.port,
                status: "available".into(),
                is_trusted: false,
                protocol: observation.protocol.clone(),
                protocols: Vec::new(),
                services: Vec::new(),
                last_seen_unix_secs: now,
                device_capabilities: DeviceCapabilities::unknown(),
                lab: observation.lab,
            });
        device.name = observation.name;
        device.platform = observation.platform;
        device.ip_address = observation.ip_address;
        device.port = observation.port;
        device.status = "available".into();
        device.last_seen_unix_secs = now;
        device.lab = device.lab || observation.lab;
        if !device.protocols.contains(&observation.protocol) {
            device.protocols.push(observation.protocol);
        }
        if !device.services.contains(&observation.service_name) {
            device.services.push(observation.service_name.clone());
        }
        device.protocol = preferred_protocol(&device.protocols, &device.services, device.lab);
        device.device_capabilities =
            capabilities_for_device(&device.protocol, &device.services, device.lab);
        self.service_index
            .insert(observation.service_name, device_id);
    }

    fn remove_service(&mut self, service_name: &str) {
        let Some(device_id) = self.service_index.remove(service_name) else {
            return;
        };
        if self.service_index.values().any(|id| id == &device_id) {
            if let Some(device) = self.devices.get_mut(&device_id) {
                device.services.retain(|service| service != service_name);
            }
        } else {
            self.devices.remove(&device_id);
        }
    }

    fn prune_expired(&mut self) {
        let now = unix_seconds();
        let stale: HashSet<_> = self
            .devices
            .iter()
            .filter(|(_, device)| {
                now.saturating_sub(device.last_seen_unix_secs) > DEVICE_TTL.as_secs()
            })
            .map(|(id, _)| id.clone())
            .collect();
        self.devices.retain(|id, _| !stale.contains(id));
        self.service_index.retain(|_, id| !stale.contains(id));
    }
}

fn ssdp_name(response: &str) -> String {
    let response = response.to_ascii_lowercase();
    if response.contains("webos") || response.contains("lg") {
        "LG Smart TV".into()
    } else if response.contains("tizen") || response.contains("samsung") {
        "Samsung Smart TV".into()
    } else if response.contains("sony") {
        "Sony BRAVIA TV".into()
    } else if response.contains("philips") {
        "Philips Smart TV".into()
    } else {
        "Smart TV (DLNA)".into()
    }
}

fn unix_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn preferred_protocol(
    protocols: &[DetectedProtocol],
    services: &[String],
    lab: bool,
) -> DetectedProtocol {
    use crate::receivers::airplay::airplay_lab_sender_available;
    use crate::receivers::miracast::{fluxcast_available, wfd_lab_sender_available};
    use crate::router::{select_protocol, RouteInput};

    let airplay_lab = lab
        || services
            .iter()
            .any(|service| service.contains("_airplay-lab._tcp"));
    let miracast_lab = lab
        || services
            .iter()
            .any(|service| service.contains("_wfd-lab._tcp"));

    if let Some(selected) = select_protocol(&RouteInput {
        advertised: protocols.to_vec(),
        google_cast_ready: protocols.contains(&DetectedProtocol::GoogleCast),
        miracast_ready: (fluxcast_available() || (wfd_lab_sender_available() && miracast_lab))
            && protocols.contains(&DetectedProtocol::Miracast),
        nearby_cast_ready: protocols.contains(&DetectedProtocol::NearbyCast),
        // Only prefer AirPlay when a lab-capable sink is present; FairPlay
        // Apple TVs remain advertised-only until physical verification.
        airplay_ready: airplay_lab_sender_available()
            && airplay_lab
            && protocols.contains(&DetectedProtocol::AirPlay),
    }) {
        return selected;
    }

    [
        DetectedProtocol::GoogleCast,
        DetectedProtocol::NearbyCast,
        DetectedProtocol::Miracast,
        DetectedProtocol::AirPlay,
        DetectedProtocol::Unknown,
    ]
    .iter()
    .find(|candidate| protocols.contains(candidate))
    .cloned()
    .unwrap_or(DetectedProtocol::Unknown)
}

fn capabilities_for_device(
    protocol: &DetectedProtocol,
    services: &[String],
    lab: bool,
) -> DeviceCapabilities {
    match protocol {
        DetectedProtocol::GoogleCast => DeviceCapabilities::for_google_cast(),
        DetectedProtocol::NearbyCast => DeviceCapabilities::for_nearby_cast(),
        DetectedProtocol::AirPlay => {
            if lab
                || services
                    .iter()
                    .any(|service| service.contains("_airplay-lab._tcp"))
            {
                DeviceCapabilities::for_airplay_lab()
            } else {
                DeviceCapabilities::for_airplay()
            }
        }
        DetectedProtocol::Miracast => {
            if lab
                || services
                    .iter()
                    .any(|service| service.contains("_wfd-lab._tcp"))
            {
                DeviceCapabilities::for_miracast_lab()
            } else {
                DeviceCapabilities::for_miracast()
            }
        }
        DetectedProtocol::Unknown => DeviceCapabilities::unknown(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn observation(service_name: &str, protocol: DetectedProtocol) -> DiscoveryObservation {
        DiscoveryObservation {
            stable_identity: "receiver-1".into(),
            service_name: service_name.into(),
            name: "Receiver".into(),
            platform: protocol.to_string(),
            ip_address: "192.168.1.40".into(),
            port: 8009,
            protocol,
            lab: service_name.contains("-lab"),
        }
    }

    #[test]
    fn airplay_lab_is_castable_when_sender_exists() {
        let mut registry = DeviceRegistry::default();
        registry.upsert(observation(
            "Test._airplay-lab._tcp.local.",
            DetectedProtocol::AirPlay,
        ));
        let device = registry.devices.values().next().unwrap();
        assert!(device.lab);
        assert!(device.device_capabilities.screen_cast);
        assert_eq!(
            device.device_capabilities.verification,
            "simulator_verified"
        );
    }

    #[test]
    fn coalesces_multiple_advertisements_for_one_device() {
        let mut registry = DeviceRegistry::default();
        registry.upsert(observation("airplay", DetectedProtocol::AirPlay));
        registry.upsert(observation("googlecast", DetectedProtocol::GoogleCast));
        let device = registry.devices.values().next().unwrap();
        assert_eq!(registry.devices.len(), 1);
        assert_eq!(device.protocol, DetectedProtocol::GoogleCast);
        assert_eq!(device.protocols.len(), 2);
        assert_eq!(device.services.len(), 2);
    }

    #[test]
    fn removes_only_after_the_last_service_disappears() {
        let mut registry = DeviceRegistry::default();
        registry.upsert(observation("one", DetectedProtocol::AirPlay));
        registry.upsert(observation("two", DetectedProtocol::GoogleCast));
        registry.remove_service("one");
        assert_eq!(registry.devices.len(), 1);
        registry.remove_service("two");
        assert!(registry.devices.is_empty());
    }
}
