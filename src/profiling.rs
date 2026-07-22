//! Optional profiling: per-stage timings, per-thread breakdown, and
//! memory-usage sampling — disabled by default, enabled with `-x`/
//! `--profile` (see [`crate::params::Params`]).
//!
//! # Design
//!
//! Everything is accumulated into in-memory buffers (`Mutex<Vec<_>>`) and
//! serialized to a single JSON file only once, at the very end of the run
//! ([`Profiler::finish_and_write`]) — the hot mapping/indexing path never
//! does file I/O for this. When `--profile` isn't passed, [`Profiler::new`]
//! returns a `Profiler` with `enabled = false`; every recording method
//! checks that flag *first* and returns immediately, so a normal run pays
//! no `Instant::now()`, no `/proc` read, and no allocation for any of this
//! — the existing [`crate::utils::Timers`]/[`crate::utils::Counters`]
//! instrumentation (always-on, and already cheap) is unaffected either way.
//!
//! # What gets recorded
//!
//! - `global`: the run-wide [`Timers`]/[`Counters`] (same numbers
//!   `print_time_stats`/`print_stats` already print to stderr), so the JSON
//!   is a strict superset of the existing stderr summary.
//! - `threads`: one entry per actual OS thread that did work — the
//!   indexer, the FASTA reader thread, each mapping worker, and the
//!   collector — each with its *own* (not globally merged) timers/counters.
//!   This is what makes the report meaningful for `-@ N > 1`: it shows load
//!   balance across workers and whether the single-threaded collector
//!   (output/PAF writing, strictly serial by construction) is a bottleneck,
//!   neither of which is visible in the merged `global` numbers alone.
//! - `memory`: periodic (every [`MEM_SAMPLE_INTERVAL`]) RSS/peak-RSS
//!   snapshots from `/proc/self/status` via a dedicated background thread
//!   (so sampling never blocks or is blocked by the actual work), plus
//!   named marks (`"start"`, `"after_index"`, `"after_mapping"`, ...) that
//!   line up the memory timeline with the phase timeline.
//!
//! Linux-only (`/proc/self/status`); this matches the project's existing
//! Linux-only memory-instrumentation precedent (see `Handler`'s doc comment
//! on the now-dropped `printMemoryUsage`) and its benchmarking environment
//! (Ubuntu, see `BENCHMARKS.md`). On other platforms the memory samples are
//! simply all-zero rather than a build failure.

use std::fmt::Write as _;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::utils::{Counters, Timers};

/// How often the background thread samples `/proc/self/status`. Coarse on
/// purpose: fine enough to see which phase memory grows in, coarse enough
/// that the sampler thread itself is never a meaningful CPU/contention cost.
const MEM_SAMPLE_INTERVAL: Duration = Duration::from_millis(200);

struct MemSample {
    at_secs: f64,
    /// `Some(name)` for an explicit [`Profiler::mem_mark`] call, `None` for
    /// a periodic background sample.
    label: Option<String>,
    rss_kb: u64,
    vmhwm_kb: u64,
}

/// One OS thread's own timers/counters, captured independently of the
/// merged run-wide totals — see the module doc comment.
struct ThreadProfile {
    label: String,
    role: &'static str,
    /// Reads (for the reader/workers) or reads-applied (for the collector)
    /// this thread handled; `0`/unused for the indexer.
    jobs: u64,
    timers: Timers,
    counters: Counters,
}

/// Best-effort `/proc/self/status` reader. Returns `(VmRSS, VmHWM)` in KB,
/// or `(0, 0)` if unavailable (non-Linux, or the file is unreadable) —
/// profiling degrades gracefully rather than failing the run.
fn sample_memory_kb() -> (u64, u64) {
    let Ok(status) = std::fs::read_to_string("/proc/self/status") else {
        return (0, 0);
    };
    let field = |name: &str| -> u64 {
        status
            .lines()
            .find(|l| l.starts_with(name))
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|v| v.parse().ok())
            .unwrap_or(0)
    };
    (field("VmRSS:"), field("VmHWM:"))
}

pub struct Profiler {
    enabled: bool,
    run_start: Instant,
    wall_clock_start: SystemTime,
    meta: Mutex<Vec<(String, String)>>,
    threads: Mutex<Vec<ThreadProfile>>,
    /// `Arc`'d (rather than borrowed) so the background sampler thread —
    /// which must be `'static`, unlike the scoped worker/reader threads
    /// elsewhere in this codebase — can hold its own handle to it.
    mem_samples: Arc<Mutex<Vec<MemSample>>>,
    stop_sampler: Arc<AtomicBool>,
    sampler: Mutex<Option<JoinHandle<()>>>,
}

impl Profiler {
    /// Constructs a profiler; if `enabled`, immediately takes a `"start"`
    /// memory mark and spawns the periodic background sampler thread. If
    /// not, this is just a few `bool`/`Instant`/`SystemTime` field writes —
    /// cheap enough to construct unconditionally in `main`.
    pub fn new(enabled: bool) -> Profiler {
        let p = Profiler {
            enabled,
            run_start: Instant::now(),
            wall_clock_start: SystemTime::now(),
            meta: Mutex::new(Vec::new()),
            threads: Mutex::new(Vec::new()),
            mem_samples: Arc::new(Mutex::new(Vec::new())),
            stop_sampler: Arc::new(AtomicBool::new(false)),
            sampler: Mutex::new(None),
        };
        if enabled {
            p.mem_mark("start");
            let mem_samples = Arc::clone(&p.mem_samples);
            let stop = Arc::clone(&p.stop_sampler);
            let run_start = p.run_start;
            let handle = std::thread::Builder::new()
                .name("shmap-profiler".to_string())
                .spawn(move || {
                    while !stop.load(Ordering::Relaxed) {
                        std::thread::sleep(MEM_SAMPLE_INTERVAL);
                        if stop.load(Ordering::Relaxed) {
                            break;
                        }
                        let (rss_kb, vmhwm_kb) = sample_memory_kb();
                        mem_samples.lock().unwrap().push(MemSample {
                            at_secs: run_start.elapsed().as_secs_f64(),
                            label: None,
                            rss_kb,
                            vmhwm_kb,
                        });
                    }
                })
                .expect("failed to spawn profiler memory-sampler thread");
            *p.sampler.lock().unwrap() = Some(handle);
        }
        p
    }

    pub fn enabled(&self) -> bool {
        self.enabled
    }

    /// Records a free-form `(key, value)` fact about this run (parameters,
    /// hardware, input sizes, ...) for correlating profiles taken on
    /// different hardware/inputs later.
    pub fn meta(&self, key: &str, value: impl std::fmt::Display) {
        if !self.enabled {
            return;
        }
        self.meta.lock().unwrap().push((key.to_string(), value.to_string()));
    }

    /// Takes an immediate, named memory snapshot (e.g. `"after_index"`) in
    /// addition to the periodic background samples, so the memory timeline
    /// can be lined up against specific phase boundaries.
    pub fn mem_mark(&self, label: &str) {
        if !self.enabled {
            return;
        }
        let (rss_kb, vmhwm_kb) = sample_memory_kb();
        self.mem_samples.lock().unwrap().push(MemSample {
            at_secs: self.run_start.elapsed().as_secs_f64(),
            label: Some(label.to_string()),
            rss_kb,
            vmhwm_kb,
        });
    }

    /// Records one OS thread's own (not globally merged) timers/counters.
    /// `role` is a short fixed tag (`"index"`, `"io"`, `"map"`, `"output"`)
    /// grouping threads by what kind of work they did.
    pub fn record_thread(&self, label: impl Into<String>, role: &'static str, jobs: u64, timers: Timers, counters: Counters) {
        if !self.enabled {
            return;
        }
        self.threads.lock().unwrap().push(ThreadProfile {
            label: label.into(),
            role,
            jobs,
            timers,
            counters,
        });
    }

    /// Stops the background sampler, takes a final `"end"` memory mark,
    /// and writes the whole report to `path` as JSON. A no-op (including no
    /// file write) if profiling isn't enabled.
    pub fn finish_and_write(&self, path: &str, global_timers: &Timers, global_counters: &Counters) -> std::io::Result<()> {
        if !self.enabled {
            return Ok(());
        }
        self.stop_sampler.store(true, Ordering::Relaxed);
        if let Some(handle) = self.sampler.lock().unwrap().take() {
            let _ = handle.join();
        }
        self.mem_mark("end");
        std::fs::write(path, self.render_json(global_timers, global_counters))
    }

    fn render_json(&self, global_timers: &Timers, global_counters: &Counters) -> String {
        let mut out = String::new();
        let _ = writeln!(out, "{{");
        let _ = writeln!(out, "  \"shmap_version\": \"{}\",", env!("CARGO_PKG_VERSION"));
        let unix_secs = self
            .wall_clock_start
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let _ = writeln!(out, "  \"started_at_unix\": {unix_secs},");
        let _ = writeln!(out, "  \"wall_seconds\": {:.6},", self.run_start.elapsed().as_secs_f64());

        let meta = self.meta.lock().unwrap();
        let _ = writeln!(out, "  \"meta\": {{");
        for (i, (k, v)) in meta.iter().enumerate() {
            let comma = if i + 1 < meta.len() { "," } else { "" };
            let _ = writeln!(out, "    \"{}\": \"{}\"{comma}", json_esc(k), json_esc(v));
        }
        let _ = writeln!(out, "  }},");
        drop(meta);

        let _ = writeln!(out, "  \"global\": {{");
        let _ = writeln!(out, "    \"timers_secs\": {},", timers_json(global_timers));
        let _ = writeln!(out, "    \"counters\": {}", counters_json(global_counters));
        let _ = writeln!(out, "  }},");

        let threads = self.threads.lock().unwrap();
        let _ = writeln!(out, "  \"threads\": [");
        for (i, tp) in threads.iter().enumerate() {
            let comma = if i + 1 < threads.len() { "," } else { "" };
            let _ = writeln!(out, "    {{");
            let _ = writeln!(out, "      \"label\": \"{}\",", json_esc(&tp.label));
            let _ = writeln!(out, "      \"role\": \"{}\",", tp.role);
            let _ = writeln!(out, "      \"jobs\": {},", tp.jobs);
            let _ = writeln!(out, "      \"timers_secs\": {},", timers_json(&tp.timers));
            let _ = writeln!(out, "      \"counters\": {}", counters_json(&tp.counters));
            let _ = writeln!(out, "    }}{comma}");
        }
        let _ = writeln!(out, "  ],");
        drop(threads);

        let samples = self.mem_samples.lock().unwrap();
        let peak_kb = samples.iter().map(|s| s.rss_kb.max(s.vmhwm_kb)).max().unwrap_or(0);
        let _ = writeln!(out, "  \"memory\": {{");
        let _ = writeln!(out, "    \"peak_rss_kb\": {peak_kb},");
        let _ = writeln!(out, "    \"samples\": [");
        for (i, s) in samples.iter().enumerate() {
            let comma = if i + 1 < samples.len() { "," } else { "" };
            match &s.label {
                Some(l) => {
                    let _ = writeln!(
                        out,
                        "      {{\"at_secs\": {:.3}, \"label\": \"{}\", \"rss_kb\": {}, \"vmhwm_kb\": {}}}{comma}",
                        s.at_secs,
                        json_esc(l),
                        s.rss_kb,
                        s.vmhwm_kb
                    );
                }
                None => {
                    let _ = writeln!(
                        out,
                        "      {{\"at_secs\": {:.3}, \"rss_kb\": {}, \"vmhwm_kb\": {}}}{comma}",
                        s.at_secs, s.rss_kb, s.vmhwm_kb
                    );
                }
            }
        }
        let _ = writeln!(out, "    ]");
        let _ = writeln!(out, "  }}");
        let _ = writeln!(out, "}}");
        out
    }
}

fn json_esc(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out
}

fn timers_json(t: &Timers) -> String {
    let mut entries: Vec<_> = t.iter_secs().collect();
    entries.sort_unstable_by_key(|(name, _)| *name);
    let mut s = String::from("{");
    for (i, (name, secs)) in entries.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        let _ = write!(s, "\"{}\":{:.6}", json_esc(name), secs);
    }
    s.push('}');
    s
}

fn counters_json(c: &Counters) -> String {
    let mut entries: Vec<_> = c.iter_counts().collect();
    entries.sort_unstable_by_key(|(name, _)| *name);
    let mut s = String::from("{");
    for (i, (name, val)) in entries.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        let _ = write!(s, "\"{}\":{}", json_esc(name), val);
    }
    s.push('}');
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disabled_profiler_records_nothing_and_writes_nothing() {
        let p = Profiler::new(false);
        p.meta("k", 15);
        p.mem_mark("start");
        p.record_thread("worker-0", "map", 10, Timers::new(), Counters::new());

        let path = std::env::temp_dir().join("shmap_profiler_test_disabled.json");
        let _ = std::fs::remove_file(&path);
        p.finish_and_write(path.to_str().unwrap(), &Timers::new(), &Counters::new()).unwrap();
        assert!(!path.exists());
    }

    #[test]
    fn enabled_profiler_writes_valid_looking_json_with_recorded_data() {
        let p = Profiler::new(true);
        p.meta("k", 15);
        p.mem_mark("after_index");

        let mut timers = Timers::new();
        timers.start("sketching");
        timers.stop("sketching");
        let mut counters = Counters::new();
        counters.inc("reads", 42);
        p.record_thread("worker-0", "map", 42, timers.clone(), counters.clone());

        let path = std::env::temp_dir().join("shmap_profiler_test_enabled.json");
        p.finish_and_write(path.to_str().unwrap(), &timers, &counters).unwrap();

        let content = std::fs::read_to_string(&path).unwrap();
        assert!(content.contains("\"shmap_version\""));
        assert!(content.contains("\"k\": \"15\""));
        assert!(content.contains("\"worker-0\""));
        assert!(content.contains("\"reads\":42"));
        assert!(content.contains("\"after_index\""));
        assert!(content.contains("\"peak_rss_kb\""));
        let _ = std::fs::remove_file(&path);
    }
}
