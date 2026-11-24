// SPDX-License-Identifier: GPL-2.0-or-later
use anyhow::{ensure, Context, Result};
use libublk::ctrl::UblkCtrl;
use rublk::raid5::{
    controller::{self, Workers},
    session::{parse_uuid, uuid_string, Config, Session, StartMode, StopOutcome},
};
use std::{path::PathBuf, sync::Arc};
#[derive(clap::Args, Debug)]
pub struct Raid5Args {
    #[command(flatten)]
    pub gen_arg: crate::args::GenAddArgs,
    /// Three original NVMe members, in their persistent member order
    #[arg(value_name = "MEMBER", num_args = 3, required = true)]
    pub members: Vec<PathBuf>,
    /// Assemble an existing metadata-backed array
    #[arg(long, conflicts_with = "create_resume")]
    pub assemble: bool,
    /// Resume interrupted creation using the original array UUID
    #[arg(long, value_name = "ARRAY_UUID")]
    pub create_resume: Option<String>,
    /// Concurrent recovery stripe transactions
    #[arg(long, default_value_t = 32)]
    pub sync_window: usize,
    /// Recovery backend bytes per second (0 means unthrottled)
    #[arg(long, default_value = "0")]
    pub sync_rate: String,
    /// Number of stripe bookkeeping shards
    #[arg(long, default_value_t = 64)]
    pub stripe_shards: usize,
    /// Limit RAID5 scratch allocations
    #[arg(long, default_value = "512MiB")]
    pub scratch_memory_limit: String,
    /// Permit regular files only for explicit tests
    #[arg(long, hide = true)]
    pub test_files: bool,
}
pub fn validate(args: &Raid5Args) -> Result<()> {
    let g = &args.gen_arg;
    ensure!(g.zero_copy, "raid5 requires --zero-copy");
    ensure!(!g.user_copy, "raid5 forbids --user-copy");
    ensure!(!g.unprivileged, "raid5 BPF requires privileged operation");
    ensure!(!g.mlock, "raid5 does not support --mlock");
    ensure!(
        !g.read_only,
        "raid5 read-only mode is not supported in this version"
    );
    ensure!(
        g.physical_block_size.is_none(),
        "raid5 derives physical geometry from its members"
    );
    ensure!(
        matches!(g.logical_block_size, None | Some(512) | Some(4096)),
        "raid5 requires 512 B or 4 KiB logical sectors"
    );
    ensure!(
        args.members.len() == 3,
        "raid5 requires exactly three members"
    );
    ensure!(
        g.queue > 0 && g.depth > 0 && g.depth <= 256,
        "invalid queue count/depth"
    );
    Ok(())
}
pub fn run(ctrl: UblkCtrl, opt: Option<Raid5Args>, comm: &Arc<crate::DevIdComm>) -> Result<i32> {
    let config = if let Some(args) = opt {
        validate(&args)?;
        let mode = if args.assemble {
            StartMode::Assemble
        } else if let Some(array) = args.create_resume {
            StartMode::Resume {
                array: parse_uuid(&array)?,
            }
        } else {
            StartMode::Create
        };
        let paths: Vec<_> = args
            .members
            .into_iter()
            .map(|p| args.gen_arg.build_abs_path(p))
            .collect();
        Config {
            paths: paths.try_into().unwrap(),
            test_files: args.test_files,
            mode,
            sync_window: args.sync_window,
            sync_rate: parse_size::parse_size(args.sync_rate)?,
            shards: args.stripe_shards,
            memory_limit: parse_size::parse_size(args.scratch_memory_limit)?.try_into()?,
            expected_uuid: None,
            logical_sector: args.gen_arg.logical_block_size,
        }
    } else {
        let json = ctrl
            .get_target_data_from_json()
            .context("missing RAID5 discovery JSON")?;
        let mut config: Config = serde_json::from_value(json["raid5"]["config"].clone())?;
        ensure!(
            config.expected_uuid.is_some(),
            "recovery JSON lacks original UUID"
        );
        config.mode = StartMode::Assemble;
        config
    };
    let info = ctrl.dev_info();
    let session = Session::prepare(
        config,
        info.nr_hw_queues as usize,
        info.queue_depth as usize,
        info.max_io_buf_bytes as usize,
    )?;
    rublk::raid5::log(format_args!("RAID5 STARTING uuid={} generation={} frontier={}/{} arena_bytes={} descriptors={} kernel_request_bound={}",uuid_string(session.identities[0].array),session.generation,session.shared.first_unsynced(),session.shared.geometry.stripes(),session.budget.arena_bytes,session.budget.descriptor_bytes,session.budget.kernel_request_bytes));
    let workers = Workers {
        sync_window: session.config.sync_window,
        sync_rate: session.config.sync_rate,
    };
    let proof = controller::run(
        &ctrl,
        session.backends.clone(),
        session.shared.clone(),
        workers,
        session.json(),
        |ctrl| {
            comm.send_dev_id(ctrl.dev_info().dev_id)?;
            Ok(())
        },
    )?;
    let publisher = rublk::raid5::status::Publisher::new(
        info.dev_id,
        session.identities[0].array,
        session.generation,
    );
    let shared = session.shared.clone();
    match session.finish(proof)? {
        StopOutcome::Clean { generation } => {
            publisher.publish(&shared, Some("STOPPED_CLEAN"), Some(generation))?;
            rublk::raid5::log(format_args!("RAID5 CLEAN generation={generation}"))
        }
        StopOutcome::Dirty => {
            publisher.publish(&shared, Some("STOPPED_DIRTY"), None)?;
            rublk::raid5::log(format_args!(
                "RAID5 stopped DIRTY; next assembly requires full sync"
            ))
        }
    }
    Ok(0)
}

#[derive(clap::Args, Debug)]
pub struct SyncArgs {
    #[arg(short = 'n', long)]
    pub number: u32,
    #[arg(long,conflicts_with_all=["resume","rate"])]
    pub pause: bool,
    #[arg(long,conflicts_with_all=["pause","rate"])]
    pub resume: bool,
    /// Total recovery backend bytes/s; 0 disables throttling
    #[arg(long,conflicts_with_all=["pause","resume"])]
    pub rate: Option<String>,
}
pub fn sync_command(args: SyncArgs) -> Result<i32> {
    use rublk::raid5::control::{self, Action};
    let action = if args.pause {
        Some(Action::Pause)
    } else if args.resume {
        Some(Action::Resume)
    } else if let Some(rate) = args.rate {
        Some(Action::Rate(parse_size::parse_size(rate)?))
    } else {
        None
    };
    println!(
        "{}",
        serde_json::to_string_pretty(&control::command(args.number, action)?)?
    );
    Ok(0)
}
