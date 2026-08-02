//! Measures actual package-wide downclocking on this host under sustained
//! multi-core AVX-512 use, vs scalar, for Q4/Q1 — raised mid-Q4 investigation
//! (this host is Cascade Lake, which throttles under heavy AVX-512), then
//! used to check whether it also confounds Q1's AVX-512-vs-scalar sketching
//! comparison. `turbostat` needs root (`CAP_SYS_RAWIO`/MSR access,
//! unavailable on this account) but `/proc/cpuinfo`'s per-core MHz field
//! needs no privileges and updates live.
//!
//! Spawns one thread per core, each running a tight, real (non-optimized-away)
//! loop for a few seconds — scalar and AVX-512, at both 64 threads (the
//! production-relevant case: every worker doing the same thing at once) and 1
//! thread (Q1's actual test conditions) — while sampling `/proc/cpuinfo` every
//! 200ms. Reports the busiest core's mean/min/max frequency during each run.
//!
//! Build and run: `rustc -O -C target-cpu=native downclock_probe.rs -o
//! downclock_probe && ./downclock_probe`. Standalone, outside the cargo
//! build — needs `target-cpu=native` for AVX-512 codegen, which the shipped
//! release binary deliberately does not use (verified via `objdump`; see
//! RESULTS.md §3 and the project's own no-AVX-512-on-Cascade-Lake guidance).
//!
//! Result on `a2`, reproduced twice: all-64-cores scalar holds a flat 2800
//! MHz; all-64-cores AVX-512 drops to ~2300-2460 MHz (once landing exactly on
//! the 2300 MHz base clock, zero turbo) -- a 14-18% package-wide cut. Single
//! core (63 idle) holds full 3900 MHz turbo for *both* scalar and AVX-512,
//! twice -- so Q1's single-threaded probe comparison was not itself
//! confounded, but cannot see this separate, only-appears-at-scale cost.
#![allow(unsafe_op_in_unsafe_fn)]
use std::arch::x86_64::*;
use std::fs;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

fn read_mhz() -> Vec<f64> {
    let text = fs::read_to_string("/proc/cpuinfo").unwrap_or_default();
    text.lines()
        .filter(|l| l.starts_with("cpu MHz"))
        .filter_map(|l| l.split(':').nth(1))
        .filter_map(|v| v.trim().parse::<f64>().ok())
        .collect()
}

fn run_scalar(stop: Arc<AtomicBool>) {
    let mut x: u64 = 0x1234_5678_9abc_def0;
    while !stop.load(Ordering::Relaxed) {
        for _ in 0..1_000_000u32 {
            x = x.rotate_left(13) ^ x.wrapping_mul(0x9E3779B97F4A7C15);
        }
        std::hint::black_box(x);
    }
}

#[target_feature(enable = "avx512f")]
unsafe fn avx512_body(stop: &AtomicBool) {
    let mut v = _mm512_set1_epi64(0x1234_5678_9abc_def0u64 as i64);
    let k = _mm512_set1_epi64(0x9E3779B97F4A7C15u64 as i64);
    while !stop.load(Ordering::Relaxed) {
        for _ in 0..1_000_000u32 {
            v = _mm512_xor_si512(_mm512_rol_epi64::<13>(v), _mm512_mullo_epi64(v, k));
        }
        std::hint::black_box(v);
    }
}

fn run_avx512(stop: Arc<AtomicBool>) {
    unsafe { avx512_body(&stop) }
}

fn sample_during<F>(label: &str, n_cores: usize, spawn_one: F)
where
    F: Fn(Arc<AtomicBool>) + Send + Sync + 'static + Copy,
{
    let stop = Arc::new(AtomicBool::new(false));
    let mut handles = Vec::new();
    for _ in 0..n_cores {
        let stop = stop.clone();
        handles.push(thread::spawn(move || spawn_one(stop)));
    }
    // Let it ramp up before sampling.
    thread::sleep(Duration::from_millis(500));
    let mut samples: Vec<Vec<f64>> = Vec::new();
    let t0 = Instant::now();
    while t0.elapsed() < Duration::from_secs(3) {
        samples.push(read_mhz());
        thread::sleep(Duration::from_millis(200));
    }
    stop.store(true, Ordering::Relaxed);
    for h in handles {
        let _ = h.join();
    }
    // The busiest core in each sample -- for the n_cores=1 case this isolates
    // the one active core's own frequency from the 63 idle ones, which a flat
    // mean across all 64 would otherwise dilute to near-idle.
    let busiest: Vec<f64> = samples
        .iter()
        .map(|s| s.iter().cloned().fold(f64::NEG_INFINITY, f64::max))
        .collect();
    let mean = busiest.iter().sum::<f64>() / busiest.len() as f64;
    let min = busiest.iter().cloned().fold(f64::INFINITY, f64::min);
    let max = busiest.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    println!(
        "{label}: busiest-core mean={mean:.0} MHz  min={min:.0}  max={max:.0}  ({} samples, {n_cores} thread(s))",
        busiest.len()
    );
}

fn main() {
    if !is_x86_feature_detected!("avx512f") {
        println!("no avx512f on this host");
        return;
    }
    let n = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1);
    println!("cores available: {n}\n");

    println!("--- idle baseline ---");
    let idle: Vec<f64> = read_mhz();
    let mean = idle.iter().sum::<f64>() / idle.len() as f64;
    println!("idle mean={mean:.0} MHz\n");

    println!("--- all {n} cores, scalar (u64 rotate/xor/mul) ---");
    sample_during("scalar", n, run_scalar);
    println!();

    println!("--- all {n} cores, AVX-512 (zmm rotate/xor/mul, 8 lanes) ---");
    sample_during("avx512", n, run_avx512);
    println!();

    println!("--- single core, scalar (63 others idle — Q1's actual test conditions) ---");
    sample_during("scalar-1core", 1, run_scalar);
    println!();

    println!("--- single core, AVX-512 (63 others idle — Q1's actual test conditions) ---");
    sample_during("avx512-1core", 1, run_avx512);
}
