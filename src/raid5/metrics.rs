// SPDX-License-Identifier: GPL-2.0-or-later
//! Optional per-thread diagnostic timings. Absent from ordinary release builds.
//! Durations include scheduler delay; BPF SQE latency is NOT BPF CPU time.
use std::sync::{Mutex, MutexGuard};
#[cfg(feature = "raid5-metrics")]
use std::{cell::RefCell, time::Instant};
#[derive(Clone, Copy)]
pub enum Kind {
    StripeWait,
    StripeHold,
    GateWait,
    GateHold,
    SharedAdmission,
    ExclusiveAdmission,
    StripeAdmission,
    Request,
    BpfCqe,
    Cqe,
}
#[cfg(feature = "raid5-metrics")]
thread_local! { static COUNTS: RefCell<[[u64; 64]; 10]> = const { RefCell::new([[0; 64]; 10]) }; }
#[cfg(feature = "raid5-metrics")]
struct Progress {
    started: Option<Instant>,
    last_second: u64,
    cqes: u64,
    samples: Vec<(u64, u64)>,
    truncated: bool,
}
#[cfg(feature = "raid5-metrics")]
thread_local! { static PROGRESS: RefCell<Progress> = const { RefCell::new(Progress {started:None,last_second:0,cqes:0,samples:Vec::new(),truncated:false}) }; }
#[cfg(feature = "raid5-metrics")]
impl Progress {
    fn push(&mut self, ns: u64, count: u64) {
        if self.samples.len() < 4096 {
            self.samples.push((ns, count));
        } else {
            *self.samples.last_mut().unwrap() = (ns, count);
            self.truncated = true;
        }
    }
}
pub struct Timer {
    #[cfg(feature = "raid5-metrics")]
    start: Instant,
}
impl Timer {
    #[inline]
    pub fn start() -> Self {
        Self {
            #[cfg(feature = "raid5-metrics")]
            start: Instant::now(),
        }
    }
    #[inline]
    pub fn record(self, _kind: Kind) {
        #[cfg(feature = "raid5-metrics")]
        {
            let ns = self.start.elapsed().as_nanos().min(u64::MAX as u128) as u64;
            let bucket = if ns == 0 {
                0
            } else {
                (64 - ns.leading_zeros()) as usize
            }
            .min(63);
            COUNTS.with(|c| c.borrow_mut()[_kind as usize][bucket] += 1);
            if matches!(_kind, Kind::Cqe) {
                PROGRESS.with(|p| {
                    let mut p = p.borrow_mut();
                    p.cqes += 1;
                    if let Some(start) = p.started {
                        let ns = start.elapsed().as_nanos().min(u64::MAX as u128) as u64;
                        let sec = ns / 1_000_000_000;
                        if sec > p.last_second {
                            let count = p.cqes;
                            p.push(ns, count);
                            p.last_second = sec;
                        }
                    }
                });
            }
        }
    }
}
pub struct Guard<'a, T> {
    guard: Option<MutexGuard<'a, T>>,
    timer: Timer,
    kind: Kind,
}
impl<T> std::ops::Deref for Guard<'_, T> {
    type Target = T;
    fn deref(&self) -> &T {
        self.guard.as_ref().unwrap()
    }
}
impl<T> std::ops::DerefMut for Guard<'_, T> {
    fn deref_mut(&mut self) -> &mut T {
        self.guard.as_mut().unwrap()
    }
}
impl<T> Drop for Guard<'_, T> {
    fn drop(&mut self) {
        // Release before recording: no allocation/logging/metrics callback under mutex.
        drop(self.guard.take());
        let timer = std::mem::replace(&mut self.timer, Timer::start());
        timer.record(self.kind);
    }
}
#[inline]
pub fn lock<T>(mutex: &Mutex<T>, stripe: bool) -> Guard<'_, T> {
    let timer = Timer::start();
    let guard = mutex.lock().unwrap();
    timer.record(if stripe {
        Kind::StripeWait
    } else {
        Kind::GateWait
    });
    Guard {
        guard: Some(guard),
        timer: Timer::start(),
        kind: if stripe {
            Kind::StripeHold
        } else {
            Kind::GateHold
        },
    }
}
pub struct Scope {
    #[cfg(feature = "raid5-metrics")]
    label: String,
}
impl Scope {
    pub fn new(_label: impl FnOnce() -> String) -> Self {
        #[cfg(feature = "raid5-metrics")]
        {
            COUNTS.with(|c| *c.borrow_mut() = [[0; 64]; 10]);
            PROGRESS.with(|p| {
                *p.borrow_mut() = Progress {
                    started: Some(Instant::now()),
                    last_second: 0,
                    cqes: 0,
                    samples: Vec::new(),
                    truncated: false,
                }
            });
        }
        Self {
            #[cfg(feature = "raid5-metrics")]
            label: _label(),
        }
    }
}
impl Drop for Scope {
    fn drop(&mut self) {
        #[cfg(feature = "raid5-metrics")]
 COUNTS.with(|c| {let c=c.borrow();
   let names=["stripe_mutex_wait","stripe_mutex_hold","gate_mutex_wait","gate_mutex_hold","shared_admission","exclusive_admission","stripe_admission","request","bpf_cqe","backend_bpf_cqe"];
   let hist:serde_json::Map<String,serde_json::Value>=names.iter().zip(c.iter()).map(|(n,b)|(n.to_string(),serde_json::json!(b.to_vec()))).collect();
   let (progress,progress_truncated)=PROGRESS.with(|p| {let mut p=p.borrow_mut();let ns=p.started.map(|s|s.elapsed().as_nanos().min(u64::MAX as u128) as u64).unwrap_or(0);let count=p.cqes;p.push(ns,count);(serde_json::json!(p.samples),p.truncated)});
   let line=format!("RAID5_METRICS {}\n",serde_json::json!({"label":self.label,"bucket":"index 0 = 0 ns; index i = [2^(i-1),2^i) ns, final bucket saturated","histograms":hist,"backend_bpf_cqe_progress_elapsed_ns":progress,"progress_truncated":progress_truncated}));
   use std::io::Write;
   let _ = std::io::stderr().lock().write_all(line.as_bytes());
 });
    }
}
