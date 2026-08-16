use serde::{Deserialize, Serialize};
use std::time::Instant;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum CastState {
    Idle,
    Discovering,
    Connecting {
        target: String,
    },
    Authenticating {
        target: String,
    },
    WaitingForReceiver {
        target: String,
    },
    PreparingCapture,
    StartingStream {
        target: String,
    },
    Casting {
        target: String,
        duration_secs: u64,
    },
    /// Bounded recovery after an unexpected disconnect. Never infinite.
    Reconnecting {
        target: String,
        attempt: u32,
        max_attempts: u32,
        reason: String,
    },
    Disconnecting,
    Failed {
        stage: String,
        reason: String,
    },
}

pub struct StateTracker {
    current_state: CastState,
    start_time: Option<Instant>,
}

impl Default for StateTracker {
    fn default() -> Self {
        Self::new()
    }
}

impl StateTracker {
    pub fn new() -> Self {
        Self {
            current_state: CastState::Idle,
            start_time: None,
        }
    }

    pub fn set_state(&mut self, state: CastState) {
        println!("[STATE_CHANGE] {:?}", state);
        if let CastState::Casting { .. } = state {
            if self.start_time.is_none() {
                self.start_time = Some(Instant::now());
            }
        } else if let CastState::Idle | CastState::Failed { .. } = state {
            self.start_time = None;
        }
        self.current_state = state;
    }

    pub fn get_state(&self) -> CastState {
        let mut state = self.current_state.clone();
        if let CastState::Casting { ref target, .. } = state {
            let duration = self.start_time.map(|t| t.elapsed().as_secs()).unwrap_or(0);
            state = CastState::Casting {
                target: target.clone(),
                duration_secs: duration,
            };
        }
        state
    }
}
