// SPDX-License-Identifier: GPL-2.0-or-later
pub mod backend;
pub mod bpf_runtime;
pub mod buffer;
pub mod controller;
pub mod engine;
pub mod flush_gate;
pub mod geometry;
pub mod metadata;
pub mod queue;
pub mod queue_wake;
pub mod recovery;
pub mod session;
pub mod startup;
pub mod status;
pub mod stripe;
pub mod sync_worker;
pub mod transaction;

/// Diagnostic output must never panic while an original CQE owns resources.
pub fn log(args: std::fmt::Arguments<'_>) {
    use std::io::Write;
    let _ = writeln!(std::io::stderr().lock(), "{args}");
}

pub mod control;
pub mod sync_control;

pub mod metrics;
