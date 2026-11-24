// SPDX-License-Identifier: GPL-2.0-or-later
//! Pause admission and in-flight accounting share one mutex: a pause ACK cannot
//! race a newly admitted scanner transaction. Foreground repair never pauses.
use std::sync::Mutex;
#[derive(Clone, Copy, serde::Serialize)]
pub struct Snapshot {
    pub requested: bool,
    pub paused: bool,
    pub active: usize,
    pub rate: u64,
    pub epoch: u64,
}
struct State {
    paused: bool,
    active: usize,
    rate: u64,
    epoch: u64,
}
pub struct SyncControl(Mutex<State>);
impl Default for SyncControl {
    fn default() -> Self {
        Self(Mutex::new(State {
            paused: false,
            active: 0,
            rate: 0,
            epoch: 0,
        }))
    }
}
impl SyncControl {
    pub fn snapshot(&self) -> Snapshot {
        let s = self.0.lock().unwrap();
        Snapshot {
            requested: s.paused,
            paused: s.paused && s.active == 0,
            active: s.active,
            rate: s.rate,
            epoch: s.epoch,
        }
    }
    pub fn pause(&self, value: bool) {
        let mut s = self.0.lock().unwrap();
        s.paused = value;
        s.epoch = s.epoch.wrapping_add(1);
    }
    pub fn rate(&self, value: u64) {
        let mut s = self.0.lock().unwrap();
        s.rate = value;
        s.epoch = s.epoch.wrapping_add(1);
    }
    pub fn begin(&self) -> Option<Lease<'_>> {
        let mut s = self.0.lock().unwrap();
        if s.paused {
            return None;
        }
        s.active += 1;
        Some(Lease(self))
    }
}
pub struct Lease<'a>(&'a SyncControl);
impl Drop for Lease<'_> {
    fn drop(&mut self) {
        let mut s = self.0 .0.lock().unwrap();
        assert!(s.active > 0);
        s.active -= 1;
    }
}
