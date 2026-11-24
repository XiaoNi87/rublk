// SPDX-License-Identifier: GPL-2.0-or-later
//! Isolated coordination diagnostic, never an end-to-end IOPS claim.
use rublk::raid5::{
    flush_gate::{FlushGate, Mode},
    metrics::{Kind, Scope, Timer},
    stripe::StripeWorkTable,
};
use std::{
    sync::{Arc, Barrier},
    time::{Duration, Instant},
};
fn order_probe(writers: usize, rounds: u64) {
    use futures::FutureExt;
    let start = Instant::now();
    let mut events = Vec::new();
    for round in 0..rounds {
        let gate = FlushGate::default();
        let before = smol::block_on(gate.acquire(Mode::Shared));
        events.push(serde_json::json!({"round":round,"event":"earlier_writer_granted","elapsed_ns":start.elapsed().as_nanos()}));
        let mut flush = Box::pin(gate.acquire(Mode::Exclusive));
        events.push(serde_json::json!({"round":round,"event":"flush_enqueued","elapsed_ns":start.elapsed().as_nanos()}));
        let mut later: Vec<_> = (0..writers)
            .map(|_| Box::pin(gate.acquire(Mode::Shared)))
            .collect();
        assert!(flush.as_mut().now_or_never().is_none());
        for (writer, waiter) in later.iter_mut().enumerate() {
            assert!(waiter.as_mut().now_or_never().is_none());
            events.push(serde_json::json!({"round":round,"writer":writer,"event":"later_writer_blocked_before_drain","elapsed_ns":start.elapsed().as_nanos()}));
        }
        drop(before);
        events.push(serde_json::json!({"round":round,"event":"earlier_writer_drained","elapsed_ns":start.elapsed().as_nanos()}));
        let exclusive = smol::block_on(flush);
        events.push(serde_json::json!({"round":round,"event":"flush_granted","elapsed_ns":start.elapsed().as_nanos()}));
        for (writer, waiter) in later.iter_mut().enumerate() {
            assert!(waiter.as_mut().now_or_never().is_none());
            events.push(serde_json::json!({"round":round,"writer":writer,"event":"later_writer_blocked_during_flush","elapsed_ns":start.elapsed().as_nanos()}));
        }
        drop(exclusive);
        events.push(serde_json::json!({"round":round,"event":"flush_released","elapsed_ns":start.elapsed().as_nanos()}));
        for (writer, waiter) in later.into_iter().enumerate() {
            let permit = smol::block_on(waiter);
            events.push(serde_json::json!({"round":round,"writer":writer,"event":"later_writer_granted","elapsed_ns":start.elapsed().as_nanos()}));
            drop(permit);
        }
    }
    println!(
        "{}",
        serde_json::json!({"scope":"isolated production FlushGate ordering; no backend/fsync/md ordering claim","rounds":rounds,"later_writers":writers,"passed":true,"events":events})
    );
}

fn main() {
    let args: Vec<_> = std::env::args().collect();
    assert_eq!(args.len(), 6, "shards threads pattern seconds flush_every");
    let shards: usize = args[1].parse().unwrap();
    let threads: usize = args[2].parse().unwrap();
    let pattern = &args[3];
    let seconds: u64 = args[4].parse().unwrap();
    let flush_every: u64 = args[5].parse().unwrap();
    assert!(threads > 0 && seconds > 0);
    if pattern == "order" {
        order_probe(threads, seconds);
        return;
    }
    assert!(["gate", "disjoint", "hot"].contains(&pattern.as_str()));
    let table = StripeWorkTable::new(shards);
    let gate = FlushGate::default();
    let barrier = Arc::new(Barrier::new(threads));
    let start = Instant::now();
    let count = std::thread::scope(|scope| {
        let handles: Vec<_> = (0..threads)
            .map(|tid| {
                let table = table.clone();
                let gate = gate.clone();
                let barrier = barrier.clone();
                scope.spawn(move || {
                    let _metrics = Scope::new(|| format!("coord-{tid}"));
                    barrier.wait();
                    let start = Instant::now();
                    let mut n = 0u64;
                    smol::block_on(async {
                        while start.elapsed() < Duration::from_secs(seconds) {
                            let timer = Timer::start();
                            let mode = if tid == 0 && flush_every > 0 && n % flush_every == 0 {
                                Mode::Exclusive
                            } else {
                                Mode::Shared
                            };
                            let _admission = gate.acquire(mode).await;
                            if mode == Mode::Shared && pattern != "gate" {
                                let _stripe = table
                                    .acquire(if pattern == "hot" {
                                        0
                                    } else {
                                        tid as u64 * 65536 + n % 4096
                                    })
                                    .await
                                    .unwrap();
                                smol::future::yield_now().await;
                            }
                            timer.record(Kind::Request);
                            n += 1;
                        }
                    });
                    n
                })
            })
            .collect();
        handles.into_iter().map(|h| h.join().unwrap()).sum::<u64>()
    });
    println!(
        "{}",
        serde_json::json!({"scope":"isolated coordination, no backend/BPF I/O","shards":shards,"threads":threads,"pattern":pattern,"flush_every":flush_every,"operations":count,"elapsed_ns":start.elapsed().as_nanos()})
    );
}
