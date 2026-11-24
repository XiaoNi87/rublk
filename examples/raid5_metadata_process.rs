// SPDX-License-Identifier: GPL-2.0-or-later
//! Real metadata restart fixture. No device is published and no CLEAN is forged.
use rublk::raid5::session::{Config, Session, StartMode};
fn main() -> anyhow::Result<()> {
    let paths: Vec<_> = std::env::args_os()
        .skip(1)
        .map(std::path::PathBuf::from)
        .collect();
    anyhow::ensure!(paths.len() == 3, "three test files required");
    let session = Session::prepare(
        Config {
            paths: paths.try_into().unwrap(),
            test_files: true,
            mode: StartMode::Assemble,
            sync_window: 1,
            sync_rate: 0,
            shards: 64,
            memory_limit: 64 * 1024 * 1024,
            expected_uuid: None,
            logical_sector: None,
        },
        1,
        1,
        524288,
    )?;
    println!("PREPARED_DIRTY {}", session.generation);
    drop(session);
    Ok(())
}
