use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use ed25519_dalek::SigningKey;
use rand::rngs::OsRng;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeviceIdentity {
    pub device_id: String,
    pub public_key_hex: String,
    /// Kept only in the local identity store. Never advertise or log this value.
    pub private_key_hex: String,
    pub created_at: u64,
}

pub struct IdentityManager {
    config_dir: PathBuf,
}

impl IdentityManager {
    pub fn new(config_dir: PathBuf) -> Self {
        Self { config_dir }
    }

    pub fn get_or_create_identity(&self) -> Result<DeviceIdentity, String> {
        let identity_file = self.config_dir.join("identity.json");
        if identity_file.exists() {
            let content = fs::read_to_string(&identity_file)
                .map_err(|e| format!("Failed to read identity file: {}", e))?;
            let identity: DeviceIdentity = serde_json::from_str(&content)
                .map_err(|e| format!("Failed to parse identity JSON: {}", e))?;
            if decode_hex(&identity.public_key_hex)
                .map(|key| key.len() == 32)
                .unwrap_or(false)
                && decode_hex(&identity.private_key_hex)
                    .map(|key| key.len() == 32)
                    .unwrap_or(false)
            {
                Ok(identity)
            } else {
                self.write_identity(&identity_file, &self.generate_new_identity()?)
            }
        } else {
            let identity = self.generate_new_identity()?;
            if let Some(parent) = identity_file.parent() {
                fs::create_dir_all(parent)
                    .map_err(|e| format!("Failed to create identity directory: {e}"))?;
            }
            self.write_identity(&identity_file, &identity)
        }
    }

    fn generate_new_identity(&self) -> Result<DeviceIdentity, String> {
        let signing_key = SigningKey::generate(&mut OsRng);
        let verifying_key = signing_key.verifying_key();
        let public_key_hex = encode_hex(&verifying_key.to_bytes());
        let private_key_hex = encode_hex(&signing_key.to_bytes());
        let id = format!("nc-{}", &public_key_hex[..16]);
        Ok(DeviceIdentity {
            device_id: id,
            public_key_hex,
            private_key_hex,
            created_at: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_err(|e| format!("System clock is before Unix epoch: {e}"))?
                .as_secs(),
        })
    }

    fn write_identity(
        &self,
        identity_file: &PathBuf,
        identity: &DeviceIdentity,
    ) -> Result<DeviceIdentity, String> {
        let content = serde_json::to_string_pretty(identity)
            .map_err(|e| format!("Failed to serialize identity: {e}"))?;
        fs::write(identity_file, content).map_err(|e| format!("Failed to save identity: {e}"))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(identity_file, fs::Permissions::from_mode(0o600))
                .map_err(|e| format!("Failed to protect identity file: {e}"))?;
        }
        Ok(identity.clone())
    }
}

fn encode_hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn decode_hex(value: &str) -> Option<Vec<u8>> {
    if !value.len().is_multiple_of(2) {
        return None;
    }
    (0..value.len())
        .step_by(2)
        .map(|index| u8::from_str_radix(&value[index..index + 2], 16).ok())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_identity_contains_real_key_material() {
        let manager = IdentityManager::new(std::env::temp_dir());
        let identity = manager.generate_new_identity().unwrap();
        assert_eq!(decode_hex(&identity.public_key_hex).unwrap().len(), 32);
        assert_eq!(decode_hex(&identity.private_key_hex).unwrap().len(), 32);
        assert!(identity.device_id.starts_with("nc-"));
    }
}
