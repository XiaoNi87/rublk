// SPDX-License-Identifier: GPL-2.0-or-later
//! Read-only lifecycle inspection and explicit takeover exclusion probe.
use anyhow::{ensure, Result};
fn main() -> Result<()> {
    let a = std::env::args().collect::<Vec<_>>();
    ensure!(
        a.len() >= 2,
        "usage: raid5_recovery_probe ID [check-fenced]"
    );
    let c = libublk::ctrl::UblkCtrl::new_simple(a[1].parse()?)?;
    let d = c.dev_info();
    if a.get(2).is_some_and(|s| s == "check-fenced") {
        ensure!(
            c.try_start_user_recover()? == -libc::EBUSY,
            "old live worker was not excluded"
        );
    }
    ensure!(
        d.flags & libublk::sys::UBLK_F_USER_RECOVERY as u64 != 0,
        "user recovery missing"
    );
    ensure!(
        d.flags & libublk::sys::UBLK_F_USER_RECOVERY_REISSUE as u64 == 0,
        "request reissue enabled"
    );
    println!(
        "{}",
        serde_json::json!({"pid":d.ublksrv_pid,"state":d.state,"flags":d.flags,"reissue":false})
    );
    Ok(())
}
