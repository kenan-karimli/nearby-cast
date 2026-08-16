//! Bounded reconnect with exponential backoff.
//!
//! Network and receiver failures are expected. Retries are capped so the UI
//! cannot stay stuck in "connecting..." forever and so orphaned helper
//! processes are not respawned indefinitely.

use serde::{Deserialize, Serialize};
use std::time::{Duration, Instant};

pub const DEFAULT_MAX_ATTEMPTS: u32 = 5;
pub const DEFAULT_BASE_DELAY_MS: u64 = 500;
pub const DEFAULT_MAX_DELAY_MS: u64 = 8_000;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReconnectPhase {
    Idle,
    Waiting,
    Attempting,
    Exhausted,
    Recovered,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReconnectSnapshot {
    pub phase: ReconnectPhase,
    pub attempt: u32,
    pub max_attempts: u32,
    pub next_delay_ms: u64,
    pub last_error: Option<String>,
}

#[derive(Debug, Clone)]
pub struct ReconnectPolicy {
    pub max_attempts: u32,
    pub base_delay: Duration,
    pub max_delay: Duration,
    attempt: u32,
    phase: ReconnectPhase,
    next_ready_at: Option<Instant>,
    last_error: Option<String>,
}

impl Default for ReconnectPolicy {
    fn default() -> Self {
        Self::new(
            DEFAULT_MAX_ATTEMPTS,
            DEFAULT_BASE_DELAY_MS,
            DEFAULT_MAX_DELAY_MS,
        )
    }
}

impl ReconnectPolicy {
    pub fn new(max_attempts: u32, base_delay_ms: u64, max_delay_ms: u64) -> Self {
        Self {
            max_attempts: max_attempts.max(1),
            base_delay: Duration::from_millis(base_delay_ms.max(1)),
            max_delay: Duration::from_millis(max_delay_ms.max(base_delay_ms.max(1))),
            attempt: 0,
            phase: ReconnectPhase::Idle,
            next_ready_at: None,
            last_error: None,
        }
    }

    pub fn reset(&mut self) {
        self.attempt = 0;
        self.phase = ReconnectPhase::Idle;
        self.next_ready_at = None;
        self.last_error = None;
    }

    pub fn mark_recovered(&mut self) {
        self.attempt = 0;
        self.phase = ReconnectPhase::Recovered;
        self.next_ready_at = None;
        self.last_error = None;
    }

    /// Record a failure and schedule the next attempt if budget remains.
    /// Returns `true` when another attempt is allowed.
    pub fn note_failure(&mut self, error: impl Into<String>) -> bool {
        self.last_error = Some(error.into());
        if self.attempt >= self.max_attempts {
            self.phase = ReconnectPhase::Exhausted;
            self.next_ready_at = None;
            return false;
        }
        let delay = self.delay_for_attempt(self.attempt);
        self.attempt += 1;
        self.phase = ReconnectPhase::Waiting;
        self.next_ready_at = Some(Instant::now() + delay);
        true
    }

    pub fn delay_for_attempt(&self, attempt: u32) -> Duration {
        let shift = attempt.min(16);
        self.base_delay
            .saturating_mul(1u32 << shift)
            .min(self.max_delay)
    }

    /// Returns `true` when a reconnect attempt should run now.
    pub fn ready_to_attempt(&mut self) -> bool {
        if self.phase == ReconnectPhase::Exhausted
            || self.attempt == 0 && self.phase == ReconnectPhase::Idle
        {
            return false;
        }
        match self.next_ready_at {
            Some(deadline) if Instant::now() >= deadline => {
                self.phase = ReconnectPhase::Attempting;
                true
            }
            Some(_) => false,
            None => self.phase == ReconnectPhase::Attempting,
        }
    }

    /// Begin the first reconnect cycle after an unexpected disconnect.
    pub fn begin(&mut self, error: impl Into<String>) -> bool {
        self.reset();
        self.note_failure(error)
    }

    pub fn is_exhausted(&self) -> bool {
        self.phase == ReconnectPhase::Exhausted
    }

    pub fn is_active(&self) -> bool {
        matches!(
            self.phase,
            ReconnectPhase::Waiting | ReconnectPhase::Attempting
        )
    }

    pub fn snapshot(&self) -> ReconnectSnapshot {
        let next_delay_ms = self
            .next_ready_at
            .map(|deadline| {
                deadline
                    .saturating_duration_since(Instant::now())
                    .as_millis() as u64
            })
            .unwrap_or(0);
        ReconnectSnapshot {
            phase: self.phase.clone(),
            attempt: self.attempt,
            max_attempts: self.max_attempts,
            next_delay_ms,
            last_error: self.last_error.clone(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread;

    #[test]
    fn backoff_grows_then_caps() {
        let policy = ReconnectPolicy::new(5, 100, 400);
        assert_eq!(policy.delay_for_attempt(0), Duration::from_millis(100));
        assert_eq!(policy.delay_for_attempt(1), Duration::from_millis(200));
        assert_eq!(policy.delay_for_attempt(2), Duration::from_millis(400));
        assert_eq!(policy.delay_for_attempt(3), Duration::from_millis(400));
    }

    #[test]
    fn exhausts_after_bounded_failures() {
        let mut policy = ReconnectPolicy::new(3, 1, 1);
        assert!(policy.begin("disconnect"));
        assert!(
            policy.ready_to_attempt() || {
                thread::sleep(Duration::from_millis(2));
                policy.ready_to_attempt()
            }
        );
        assert!(policy.note_failure("again"));
        thread::sleep(Duration::from_millis(2));
        assert!(policy.ready_to_attempt());
        assert!(policy.note_failure("third"));
        thread::sleep(Duration::from_millis(2));
        assert!(policy.ready_to_attempt());
        assert!(!policy.note_failure("fourth"));
        assert!(policy.is_exhausted());
        assert!(!policy.ready_to_attempt());
    }

    #[test]
    fn recovered_clears_budget() {
        let mut policy = ReconnectPolicy::new(2, 1, 1);
        assert!(policy.begin("down"));
        policy.mark_recovered();
        assert_eq!(policy.snapshot().phase, ReconnectPhase::Recovered);
        assert!(!policy.is_active());
    }
}
