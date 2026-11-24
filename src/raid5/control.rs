// SPDX-License-Identifier: GPL-2.0-or-later
//! Root-private, generation/PID-bound volatile scanner control. No metadata role.
use anyhow::{ensure, Result};
use std::{
    io::{Read, Write},
    os::unix::{
        fs::PermissionsExt,
        net::{UnixListener, UnixStream},
    },
    path::PathBuf,
    time::Duration,
};
#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    uuid: String,
    generation: u64,
    pid: u32,
    action: Action,
}
#[derive(serde::Serialize, serde::Deserialize)]
pub enum Action {
    Pause,
    Resume,
    Rate(u64),
}
fn path(id: u32) -> PathBuf {
    super::status::path(id).with_extension("control.sock")
}
pub struct Server {
    listener: UnixListener,
    path: PathBuf,
    uuid: String,
    generation: u64,
}
impl Server {
    pub fn bind(id: u32, uuid: [u8; 16], generation: u64) -> Result<Self> {
        let path = path(id);
        let parent = path.parent().unwrap();
        std::fs::create_dir_all(parent)?;
        std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700))?;
        // Caller already owns all original backend OFDs, excluding an old server.
        match std::fs::remove_file(&path) {
            Ok(()) => (),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            Err(e) => return Err(e.into()),
        }
        let listener = UnixListener::bind(&path)?;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
        listener.set_nonblocking(true)?;
        Ok(Self {
            listener,
            path,
            uuid: super::session::uuid_string(uuid),
            generation,
        })
    }
    pub fn poll(&self, shared: &super::engine::Shared) {
        // Bound work per controller tick even if a root client floods commands.
        for _ in 0..8 {
            let (mut stream, _) = match self.listener.accept() {
                Ok(v) => v,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(e) => {
                    super::log(format_args!("RAID5 control accept: {e}"));
                    break;
                }
            };
            let result = (|| -> Result<()> {
                stream.set_read_timeout(Some(Duration::from_millis(50)))?;
                stream.set_write_timeout(Some(Duration::from_millis(50)))?;
                let mut raw = Vec::new();
                Read::by_ref(&mut stream).take(4097).read_to_end(&mut raw)?;
                ensure!(raw.len() <= 4096, "oversized control request");
                let req: Request = serde_json::from_slice(&raw)?;
                ensure!(
                    req.uuid == self.uuid
                        && req.generation == self.generation
                        && req.pid == std::process::id(),
                    "stale RAID5 session"
                );
                ensure!(
                    !shared.stop.load(std::sync::atomic::Ordering::Acquire),
                    "device stopping"
                );
                match req.action {
                    Action::Pause => shared.sync_control.pause(true),
                    Action::Resume => shared.sync_control.pause(false),
                    Action::Rate(rate) => shared.sync_control.rate(rate),
                }
                Ok(())
            })();
            let response = match result {
                Ok(()) => serde_json::json!({"accepted":true}),
                Err(e) => serde_json::json!({"accepted":false,"error":format!("{e:#}")}),
            };
            let _ = stream.write_all(serde_json::to_string(&response).unwrap().as_bytes());
        }
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}
pub fn command(id: u32, action: Option<Action>) -> Result<serde_json::Value> {
    ensure!(
        unsafe { libc::geteuid() } == 0,
        "RAID5 control requires root"
    );
    let read = || -> Result<serde_json::Value> {
        Ok(serde_json::from_slice(&std::fs::read(
            super::status::path(id),
        )?)?)
    };
    let initial = read()?;
    if action.is_none() {
        return Ok(initial);
    }
    ensure!(
        matches!(initial["state"].as_str(), Some("RECOVERING" | "RUNNING")),
        "device is not active"
    );
    let wait_pause = matches!(action, Some(Action::Pause));
    let req = Request {
        uuid: initial["uuid"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("missing UUID"))?
            .into(),
        generation: initial["generation"]
            .as_u64()
            .ok_or_else(|| anyhow::anyhow!("missing generation"))?,
        pid: initial["pid"]
            .as_u64()
            .ok_or_else(|| anyhow::anyhow!("missing PID"))?
            .try_into()?,
        action: action.unwrap(),
    };
    let mut stream = UnixStream::connect(path(id))?;
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    stream.set_write_timeout(Some(Duration::from_secs(5)))?;
    stream.write_all(&serde_json::to_vec(&req)?)?;
    stream.shutdown(std::net::Shutdown::Write)?;
    let mut raw = Vec::new();
    stream.take(4097).read_to_end(&mut raw)?;
    ensure!(raw.len() <= 4096, "oversized response");
    let response: serde_json::Value = serde_json::from_slice(&raw)?;
    ensure!(response["accepted"] == true, "control rejected: {response}");
    for _ in 0..1000 {
        let s = read()?;
        ensure!(
            s["pid"] == initial["pid"] && s["generation"] == initial["generation"],
            "session changed during control"
        );
        let applied = match req.action {
            Action::Pause => s["sync"]["pause_requested"] == true,
            Action::Resume => s["sync"]["pause_requested"] == false,
            Action::Rate(rate) => s["sync"]["rate_bytes_per_sec"] == rate,
        };
        if applied
            && (!wait_pause
                || s["sync"]["paused"] == true
                || s["sync"]["frontier"] == s["sync"]["total_stripes"])
        {
            return Ok(s);
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    anyhow::bail!("control accepted but drain/status confirmation timed out")
}
