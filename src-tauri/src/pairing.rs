use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PairingSession {
    pub session_id: String,
    pub target_device_id: String,
    pub verification_code: String,
    pub is_confirmed: bool,
    #[serde(skip, default = "pairing_expiry")]
    expires_at: Instant,
}

fn pairing_expiry() -> Instant {
    Instant::now()
}

pub struct PairingManager {
    trusted_devices: Arc<Mutex<HashSet<String>>>,
    sessions: Arc<Mutex<HashMap<String, PairingSession>>>,
}

impl Default for PairingManager {
    fn default() -> Self {
        Self::new()
    }
}

impl PairingManager {
    pub fn new() -> Self {
        Self {
            trusted_devices: Arc::new(Mutex::new(HashSet::new())),
            sessions: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    pub fn create_pairing_session(
        &self,
        session_id: String,
        target_device_id: String,
    ) -> PairingSession {
        let code: u32 = rand::random::<u32>() % 900_000 + 100_000;
        let s = code.to_string();
        let session = PairingSession {
            session_id: session_id.clone(),
            target_device_id,
            verification_code: format!("{} {}", &s[0..3], &s[3..6]),
            is_confirmed: false,
            expires_at: Instant::now() + Duration::from_secs(120),
        };
        self.sessions
            .lock()
            .unwrap()
            .insert(session_id, session.clone());
        session
    }

    pub fn verify_and_trust(&self, session_id: &str, device_id: &str, code: &str) -> bool {
        let Some(session) = self.sessions.lock().unwrap().remove(session_id) else {
            return false;
        };
        if session.target_device_id != device_id
            || session.expires_at <= Instant::now()
            || session.verification_code != code
        {
            return false;
        }
        let mut set = self.trusted_devices.lock().unwrap();
        set.insert(device_id.to_string());
        true
    }

    pub fn is_trusted(&self, device_id: &str) -> bool {
        let set = self.trusted_devices.lock().unwrap();
        set.contains(device_id)
    }

    pub fn revoke_trust(&self, device_id: &str) {
        let mut set = self.trusted_devices.lock().unwrap();
        set.remove(device_id);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pairing_requires_matching_one_time_code_and_device() {
        let manager = PairingManager::new();
        let session = manager.create_pairing_session("session-a".into(), "receiver-a".into());
        assert!(!manager.verify_and_trust("session-a", "receiver-b", &session.verification_code));
        assert!(!manager.is_trusted("receiver-a"));

        let session = manager.create_pairing_session("session-b".into(), "receiver-a".into());
        assert!(manager.verify_and_trust("session-b", "receiver-a", &session.verification_code));
        assert!(manager.is_trusted("receiver-a"));
        assert!(!manager.verify_and_trust("session-b", "receiver-a", &session.verification_code));
    }
}
