use ed25519_dalek::{Signature, Verifier, VerifyingKey};

pub struct SecurityManager;

impl SecurityManager {
    pub fn verify_signature(data: &[u8], signature: &[u8], public_key: &[u8]) -> bool {
        let Ok(public_key) = <[u8; 32]>::try_from(public_key) else {
            return false;
        };
        let Ok(signature) = Signature::from_slice(signature) else {
            return false;
        };
        let Ok(public_key) = VerifyingKey::from_bytes(&public_key) else {
            return false;
        };
        public_key.verify(data, &signature).is_ok()
    }

    pub fn encrypt_payload(_payload: &[u8], _key: &[u8]) -> Result<Vec<u8>, String> {
        Err(
            "Payload encryption is not implemented; refusing to send plaintext as encrypted data"
                .into(),
        )
    }
}
