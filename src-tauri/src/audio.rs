use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AudioStreamConfig {
    pub system_audio_enabled: bool,
    pub microphone_enabled: bool,
    pub sample_rate: u32,
    pub channels: u16,
}

pub struct AudioEngine;

impl Default for AudioEngine {
    fn default() -> Self {
        Self::new()
    }
}

impl AudioEngine {
    pub fn new() -> Self {
        Self
    }

    pub fn configure_audio(&self, config: AudioStreamConfig) -> Result<(), String> {
        tracing::info!(
            "Audio stream configured: SystemAudio={}, Mic={}, SampleRate={}",
            config.system_audio_enabled,
            config.microphone_enabled,
            config.sample_rate
        );
        Ok(())
    }
}
