use std::process::Child;

pub struct NearbyCastSession {
    pub child: Option<Child>,
}

impl Default for NearbyCastSession {
    fn default() -> Self {
        Self::new()
    }
}

impl NearbyCastSession {
    pub fn new() -> Self {
        Self { child: None }
    }

    pub fn start_stream(
        &mut self,
        _target_ip: &str,
        _target_port: u16,
        _output_name: &str,
    ) -> Result<u32, String> {
        Err("NearbyCast authenticated streaming is not implemented; refusing to start an unauthenticated UDP stream".into())
    }

    pub fn stop(&mut self) {
        if let Some(ref mut child) = self.child {
            let _ = child.kill();
        }
        self.child = None;
    }
}
