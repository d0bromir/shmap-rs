//! The `SHMapper<NBP, OS, AP>` core mapping algorithm.
//!
//! Port of `shmap/src/shmap.h`, split by concern across this directory's
//! submodules (the original file's own method ordering already groups by
//! the same boundaries):
//! - [`seeding`]: `unique_elements_with_info`, `more_seeds_if_cheap`, `match_seeds`
//! - [`pruning`]: `hseed`, `matches_in_bucket`, `seed_heuristic_pass`
//! - [`scoring`]: `bestFixedLength`, `findBestMapping`, `lcs*`, `mappingScore`, `match_rest`
//! - [`stats`]: `print_stats`, `print_time_stats`, `print_warnings`
//!
//! `find_theta_Containment` is dropped: confirmed dead via grep (its only
//! call site is commented out, and it has no test coverage), matching this
//! port's other confirmed-dead-code removals.
//!
//! Two bugs found while porting `map_read`, both fixed here rather than
//! reproduced (per the fix-real-bugs decision made when this port was
//! planned):
//! - The C++ never actually resets `C` (its per-read `Counters`) between
//!   reads — `C.clear()` is commented out, and `C.init(...)` only lists a
//!   partial set of the counter names the function goes on to increment.
//!   Left as-is, several counters (including `total_matches`, which feeds
//!   directly into the live `total_matches:i:`/`match_inefficiency:f:` PAF
//!   tags) accumulate across every read a single `SHMapper` instance
//!   processes, then get merged into the run-wide totals via `H->C += C`
//!   after *every* read, compounding further. This port fully clears and
//!   re-registers the per-read counters each read instead.
//! - `map_read` unconditionally calls `best->set_global_stats(...)` /
//!   `best->print_paf(...)` even when `best` is `std::nullopt` (every
//!   unmapped read) — dereferencing an empty `std::optional`, which is
//!   undefined behavior in C++ and simply won't compile against `Option`
//!   in Rust. Per the decision made when this port was planned, unmapped
//!   reads get a minimal record (query id, read length, `*` fields)
//!   written to the `.unmapped.paf` file instead.
//!
//! # Multithreading (`-@`/`--threads`)
//!
//! Not present upstream at all (grep confirms zero threading in the C++).
//! `map_reads` always runs a fixed three-stage pipeline, wired up with
//! `std::thread::scope` so worker closures can borrow `tidx`/`params`/
//! `sketcher` without needing `Arc`:
//! - one reader thread streams records off disk via [`read_fasta`] and
//!   dispatches them as [`Job`]s over a bounded channel (bounding memory to
//!   a few jobs ahead of the workers);
//! - `params.threads.max(1)` worker threads each own an independent
//!   `SHMapper` + `Buckets` (per-read scratch state can't be shared across
//!   threads) and turn each `Job` into a [`ReadOutput`], sent back tagged
//!   with its original sequence index;
//! - the scope's own thread (no extra thread for this part) is the sole
//!   collector: it reorders completions by index and applies them
//!   ([`apply_read_output`], counter/timer merge, progress bar) strictly in
//!   input order, so stdout/PAF/`.unmapped.paf`/`paul.tsv` output is
//!   byte-identical regardless of thread count — only the CPU-bound mapping
//!   work actually runs in parallel.
//!
//! `-@ 1` (the default) still goes through this same pipeline rather than a
//! separate sequential fast path: with a single worker, completions already
//! arrive in submission order, so the reorder buffer is a no-op and
//! behavior is identical to before — one fewer code path to keep in sync.
//!
//! Each worker catches panics from its own `map_read` call ([`catch_read_panic`])
//! rather than letting one bad read kill the thread. Without this, a dead
//! worker stops draining the bounded job channel, the reader thread blocks
//! forever trying to send into it, and the main thread then blocks forever
//! in `reader.join()` — turning one bad read into a permanent hang instead
//! of a clean per-read error (found and fixed after reproducing exactly
//! this hang via `-v 2` against non-ground-truth-encoded reads, which
//! panics by documented design). Safe to keep the worker thread alive
//! afterward: there's no `unsafe` code anywhere in this crate, and
//! `map_read` already clears/reinitializes all of its state at the top of
//! every call, so a panic mid-read can't leave anything for the *next* read
//! on that worker to inherit.

mod pruning;
mod scoring;
mod seeding;
mod stats;

use std::collections::HashMap;
use std::io::Write;
use std::sync::mpsc;
use std::sync::Mutex;

use crate::buckets::Buckets;
use crate::handler::Handler;
use crate::io::read_fasta;
use crate::mapping::{Mapping, MappingPaf};
use crate::params::Params;
use crate::profiling::Profiler;
use crate::sketch::FracMinHash;
use crate::types::{H2Cnt, H2Seed, QPos};
use crate::utils::{Counters, ProgressBar, Timers};

/// The complete, corrected list of counter names `map_read` (and the
/// methods it calls) increments — see the module doc comment for why this
/// is a superset of the C++'s own (buggy, partial) `C.init(...)` list.
const PER_READ_COUNTERS: &[&str] = &[
    "seeds_limit_reached",
    "mapped_reads",
    "kmers",
    "kmers_notmatched",
    "seeds",
    "matches",
    "seed_matches",
    "max_seed_matches",
    "matches_freq",
    "spurious_matches",
    "mappings",
    "J_best",
    "sketched_kmers",
    "total_edit_distance",
    "intersection_diff",
    "mapq60",
    "mapq0",
    "matches_in_reported_mappings",
    "lost_on_seeding",
    "lost_on_pruning",
    "final_buckets",
    "read_len",
    "total_matches",
    "possible_matches",
    "kmers_sketched",
    "kmers_unique",
    "kmers_seeds",
    "seeded_buckets",
];

/// Everything a single `map_read` call would otherwise have written
/// straight to stdout/the unmapped-PAF file/`paul.tsv`, captured as owned
/// data instead so it can be produced on any worker thread and applied by
/// the single serial collector in [`SHMapper::map_reads`] — see the module
/// doc comment.
struct ReadOutput {
    /// Exactly what `print!` would have received for this read: the PAF
    /// line (plus verbose ground-truth tags) followed by a newline, or an
    /// empty string for an unmapped read (which prints nothing to stdout,
    /// matching the C++).
    stdout: String,
    /// The line to append to `<params_file>.unmapped.paf`, if this read
    /// didn't map.
    unmapped_line: Option<String>,
    /// The `paul.tsv` data row (verbose >= 2 only), *without* the header —
    /// header emission is centralized in [`apply_read_output`]'s caller.
    paul_row: Option<String>,
}

/// Best-effort extraction of a human-readable message from a caught panic's
/// payload — mirrors what the default panic hook prints, for the common
/// `&str`/`String` payloads `panic!`/`.unwrap()`/`.expect()` produce.
fn panic_message(payload: &(dyn std::any::Any + Send)) -> String {
    if let Some(s) = payload.downcast_ref::<&str>() {
        (*s).to_string()
    } else if let Some(s) = payload.downcast_ref::<String>() {
        s.clone()
    } else {
        "non-string panic payload".to_string()
    }
}

/// Runs one worker's `map_read` call with panics converted into a normal
/// per-read `Err`, instead of unwinding out of the worker thread — see the
/// module doc comment for why a dead worker thread is much worse than one
/// failed read (it can hang the whole pipeline, not just this read).
///
/// `worker`/`buckets` don't need any special recovery for the *next* read on
/// the caught-panic path: `map_read` already clears and reinitializes
/// `worker.timers`/`worker.counters` and calls `buckets.clear()` as the very
/// first thing it does on every call (panicked-last-time or not), and
/// there's no `unsafe` code anywhere in this crate for a mid-computation
/// panic to leave in a torn, memory-unsafe state.
///
/// This function does still clear `worker.timers`/`worker.counters` for
/// *this* (failed) read specifically, before the caller merges them into
/// the run-wide/per-thread totals: a panic can happen anywhere in
/// `map_read`, including after most of a read's real work (kmer counts,
/// `mapped_reads`, ...) already ran — merging those in would misreport a
/// read whose output was never written (the collector only applies
/// `Ok` results) as having contributed normally to the aggregate stats.
/// Emptying them makes the merge a genuine no-op instead, at the cost that
/// callers reading a counter/timer name that would otherwise only ever get
/// established via a *successful* read's contribution (e.g.
/// `handler.counters.count("mapped_reads")`, read unconditionally by the
/// collector's progress-bar check) must pre-register it themselves rather
/// than relying on the first successful read to do so — see `map_reads`'s
/// `handler.counters.init(...)` call.
#[allow(clippy::too_many_arguments)]
fn catch_read_panic<'idx, const NBP: bool, const OS: bool, const AP: bool>(
    worker: &mut SHMapper<'idx, NBP, OS, AP>,
    sketcher: &FracMinHash,
    params: &Params,
    query_id: &str,
    seq: &[u8],
    buckets: &mut Buckets<'idx, AP>,
) -> anyhow::Result<ReadOutput> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| worker.map_read(sketcher, params, query_id, seq, buckets)))
        .unwrap_or_else(|panic_payload| {
            worker.timers.clear();
            worker.counters.clear();
            Err(anyhow::anyhow!(
                "panicked while mapping read {query_id:?}: {}",
                panic_message(&*panic_payload)
            ))
        })
}

/// Applies one read's already-rendered [`ReadOutput`] — the only place that
/// actually touches stdout/the unmapped-PAF file/`paul.tsv` during mapping.
/// Must be called strictly in original read order for output to match a
/// single-threaded run.
///
/// `stdout` is a caller-owned, already-locked buffered writer rather than a
/// bare `print!()` per call: `print!`/`io::stdout()` write through a
/// `LineWriter`, which flushes on every `\n` — since every PAF line ends in
/// one, that meant a lock-and-flush (effectively a syscall) *per read*.
/// Holding one `BufWriter` open for the whole collector loop instead batches
/// many reads' output into each underlying write, which matters most on
/// fast/small datasets at high thread counts, where profiling showed the
/// collector's own share of time growing fastest (see `PROFILING.md`).
fn apply_read_output(
    output: &ReadOutput,
    stdout: &mut impl std::io::Write,
    unmapped_out: Option<&mut std::fs::File>,
    paulout: Option<&mut std::fs::File>,
    paulout_is_first_row: &mut bool,
) -> anyhow::Result<()> {
    if !output.stdout.is_empty() {
        stdout.write_all(output.stdout.as_bytes())?;
    }
    if let Some(line) = &output.unmapped_line
        && let Some(f) = unmapped_out
    {
        writeln!(f, "{line}")?;
    }
    if let Some(row) = &output.paul_row
        && let Some(f) = paulout
    {
        if *paulout_is_first_row {
            writeln!(f, "{}", crate::analyse_simulated::TSV_HEADER)?;
            *paulout_is_first_row = false;
        }
        writeln!(f, "{row}")?;
    }
    Ok(())
}

/// `NBP`/`OS`/`AP` are the C++ template bools `no_bucket_pruning`/
/// `one_sweep`/`abs_pos`. Upstream only ever compiles the `<false, false,
/// false>` instantiation (`mapper.cpp` comments out the other 7 `case`
/// arms); this port wires up all 8 as real, runtime-selectable
/// combinations — see [`crate::mapper::create_mapper`].
pub struct SHMapper<'idx, const NBP: bool, const OS: bool, const AP: bool> {
    tidx: &'idx crate::index::SketchIndex,
    /// Per-read-cycle local counters, merged into the `Handler`'s run-wide
    /// counters after each read (matching the C++'s own local `C` member,
    /// merged via `H->C += C`).
    counters: Counters,
    timers: Timers,
}

impl<'idx, const NBP: bool, const OS: bool, const AP: bool> SHMapper<'idx, NBP, OS, AP> {
    pub fn new(tidx: &'idx crate::index::SketchIndex) -> Self {
        SHMapper {
            tidx,
            counters: Counters::new(),
            timers: Timers::new(),
        }
    }

    /// Maps every read in `p_file` against the index, writing PAF to
    /// stdout (one line per mapped read) and a minimal record per unmapped
    /// read to `<params_file>.unmapped.paf` (only created/written at all
    /// if `-z`/`params_file` was given, matching the C++). Runs the
    /// reader/workers/collector pipeline described in the module doc
    /// comment, with `params.threads.max(1)` worker threads.
    pub fn map_reads(&mut self, handler: &mut Handler, p_file: &str, profiler: &Profiler) -> anyhow::Result<()> {
        // "mapped_reads" is pre-registered here (not just "reads") because
        // the collector reads it unconditionally on every completed job
        // (`handler.counters.count("mapped_reads")` below, for the progress
        // bar) — normally it only ever gets into `handler.counters` via a
        // *successful* read's own per-read `Counters` merging in, so if
        // every read up to that point failed (panicked and was caught by
        // `catch_read_panic`, which deliberately merges in nothing for a
        // failed read rather than misleading partial stats), it would
        // otherwise never exist yet and `.count(...)` would panic.
        handler.counters.init(&["reads", "mapped_reads"]);
        eprintln!("Mapping reads using SHmap...");
        let progress_bar = ProgressBar::new("Mapping");

        let mut unmapped_out = if !handler.params.params_file.is_empty() {
            let unmapped_fn = format!("{}.unmapped.paf", handler.params.params_file);
            eprintln!("Unmapped reads to {unmapped_fn}");
            Some(std::fs::File::create(&unmapped_fn)?)
        } else {
            None
        };
        let mut paulout = if !handler.params.params_file.is_empty() && handler.params.verbose >= 2 {
            let pauls_fn = format!("{}.paul.tsv", handler.params.params_file);
            eprintln!("Paul's experiment to {pauls_fn}");
            Some(std::fs::File::create(&pauls_fn)?)
        } else {
            None
        };
        let mut paulout_is_first_row = true;
        // Locked once and held for the whole collector loop (see
        // `apply_read_output`'s doc comment) instead of relocking/flushing
        // stdout on every single read.
        let stdout = std::io::stdout();
        let mut stdout_writer = std::io::BufWriter::new(stdout.lock());

        handler.timers.start("mapping");
        profiler.mem_mark("mapping_start");

        let n_threads = handler.params.threads.max(1);
        let tidx = self.tidx;
        let params = &handler.params;
        let sketcher = &handler.sketcher;

        struct Job {
            idx: u64,
            query_id: String,
            seq: Vec<u8>,
            progress: f32,
        }
        struct Done {
            idx: u64,
            progress: f32,
            counters: Counters,
            timers: Timers,
            result: anyhow::Result<ReadOutput>,
        }

        // Bounded so a fast-reading thread can't buffer the whole input
        // file ahead of slower mapping workers.
        let (job_tx, job_rx) = mpsc::sync_channel::<Job>(n_threads * 4);
        let job_rx = Mutex::new(job_rx);
        // Unbounded: a worker must never block trying to hand back a
        // finished read (the collector may be lagging behind on an earlier,
        // slower read), only the job side needs backpressure.
        let (done_tx, done_rx) = mpsc::channel::<Done>();

        let mut read_err: Option<anyhow::Error> = None;
        std::thread::scope(|scope| -> anyhow::Result<()> {
            for worker_idx in 0..n_threads {
                let job_rx = &job_rx;
                let done_tx = done_tx.clone();
                scope.spawn(move || {
                    // This thread's own cumulative timers/counters, distinct
                    // from `worker.timers`/`worker.counters` (which
                    // `map_read` clears and reinitializes every call) —
                    // reported once as a whole-thread profile below, so a
                    // profiling run can see per-worker load balance.
                    let mut thread_timers = Timers::new();
                    let mut thread_counters = Counters::new();
                    let mut jobs_done: u64 = 0;

                    // `Buckets::new` allocates one `Vec<BucketContent>` per
                    // reference segment, sized from the *whole reference*
                    // (see its doc comment) — for a multi-Gbp genome this is
                    // itself a multi-GB, multi-second allocation+zero-init,
                    // done once per worker before any per-read timer starts.
                    // Left untimed, that cost silently inflated the gap
                    // between the `mapping` phase bracket and every named
                    // per-read timer summed together; timing it here closes
                    // that blind spot.
                    if profiler.enabled() {
                        thread_timers.start("worker_setup");
                    }
                    let mut worker: SHMapper<'_, NBP, OS, AP> = SHMapper::new(tidx);
                    let mut buckets: Buckets<'_, AP> = Buckets::new(tidx);
                    if profiler.enabled() {
                        thread_timers.stop("worker_setup");
                    }
                    loop {
                        let job = job_rx.lock().unwrap().recv();
                        let Ok(job) = job else { break };
                        let result = catch_read_panic(&mut worker, sketcher, params, &job.query_id, &job.seq, &mut buckets);
                        if profiler.enabled() {
                            thread_timers += &worker.timers;
                            thread_counters += &worker.counters;
                            jobs_done += 1;
                        }
                        let done = Done {
                            idx: job.idx,
                            progress: job.progress,
                            counters: worker.counters.clone(),
                            timers: worker.timers.clone(),
                            result,
                        };
                        if done_tx.send(done).is_err() {
                            break;
                        }
                    }
                    if profiler.enabled() {
                        profiler.record_thread(format!("worker-{worker_idx}"), "map", jobs_done, thread_timers, thread_counters);
                    }
                });
            }
            drop(done_tx);

            let reader = scope.spawn(move || -> anyhow::Result<Timers> {
                let mut timers = Timers::new();
                timers.init(&["query_reading"]);
                timers.start("query_reading");
                let mut idx = 0u64;
                read_fasta(p_file, |query_id, seq, progress| {
                    timers.stop("query_reading");
                    // A send error only happens once every worker has
                    // already exited; the collector loop below will observe
                    // the closed `done_rx` and stop, so it's safe to just
                    // drop the remaining records here.
                    let _ = job_tx.send(Job {
                        idx,
                        query_id: query_id.to_string(),
                        seq: seq.to_vec(),
                        progress,
                    });
                    idx += 1;
                    timers.start("query_reading");
                })?;
                timers.stop("query_reading");
                if profiler.enabled() {
                    profiler.record_thread("reader", "io", idx, timers.clone(), Counters::new());
                }
                Ok(timers)
            });

            let mut next_idx = 0u64;
            let mut pending: HashMap<u64, Done> = HashMap::new();
            let mut collector_timers = Timers::new();
            let mut collector_counters = Counters::new();
            while let Ok(done) = done_rx.recv() {
                pending.insert(done.idx, done);
                if profiler.enabled() {
                    collector_counters.update_max("max_pending_reorder_buffer", pending.len() as i64);
                }
                while let Some(done) = pending.remove(&next_idx) {
                    if profiler.enabled() {
                        collector_timers.start("collector_busy");
                    }
                    handler.counters.inc1("reads");
                    handler.counters += &done.counters;
                    handler.timers += &done.timers;

                    match done.result {
                        Ok(output) => {
                            apply_read_output(
                                &output,
                                &mut stdout_writer,
                                unmapped_out.as_mut(),
                                paulout.as_mut(),
                                &mut paulout_is_first_row,
                            )?;
                        }
                        Err(e) => {
                            if read_err.is_none() {
                                read_err = Some(e);
                            }
                        }
                    }

                    if handler.counters.count("mapped_reads") % 100 == 0 {
                        progress_bar.update(done.progress as f64);
                    }
                    next_idx += 1;
                    if profiler.enabled() {
                        collector_timers.stop("collector_busy");
                    }
                }
            }
            if profiler.enabled() {
                profiler.record_thread("collector", "output", handler.counters.count("reads") as u64, collector_timers, collector_counters);
            }

            let reader_timers = reader.join().expect("reader thread panicked")?;
            handler.timers += &reader_timers;
            Ok(())
        })?;
        stdout_writer.flush()?;
        if let Some(e) = read_err {
            return Err(e);
        }

        handler.timers.stop("mapping");
        profiler.mem_mark("mapping_end");
        eprintln!();

        self.print_stats(handler);
        self.print_time_stats(handler);
        self.print_warnings(handler);
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn map_read(
        &mut self,
        sketcher: &FracMinHash,
        params: &Params,
        query_id: &str,
        p_seq: &[u8],
        buckets: &mut Buckets<'idx, AP>,
    ) -> anyhow::Result<ReadOutput> {
        self.counters.clear();
        self.counters.init(PER_READ_COUNTERS);
        self.timers.clear();
        self.timers.init(&["seed_heuristic", "match_collect", "refine"]);
        buckets.clear();

        self.timers.start("query_mapping");

        self.timers.start("sketching");
        let p = sketcher.sketch(p_seq, &mut self.counters);
        let m: QPos = p.len() as QPos;
        self.timers.stop("sketching");

        self.timers.start("prepare");
        self.counters.inc("read_len", p_seq.len() as i64);

        self.timers.start("seeding");
        let mut p = p;
        let p_unique = self.unique_elements_with_info(&mut p);
        self.timers.stop("seeding");

        let mut p_ht: H2Seed = H2Seed::default();
        let mut diff_hist: H2Cnt = H2Cnt::default();
        let mut possible_matches: i32 = 0;
        for seed in &p_unique {
            p_ht.insert(seed.kmer.h, seed.clone());
            diff_hist.insert(seed.kmer.h, seed.occs_in_p);
            possible_matches += seed.hits_in_t;
        }
        self.counters.inc("possible_matches", possible_matches as i64);

        let lmax: QPos = m;
        let theta = params.theta;
        let theta2 = theta - params.min_diff;
        // `one_sweep`'s best-effort interpretation (see the crate-level
        // decision this port recorded): use every unique k-mer as a seed
        // instead of the theta-derived early-cutoff count `S`.
        let s: QPos = if OS {
            p_unique.len() as QPos
        } else {
            ((1.0 - theta2) * m as f64) as QPos + 1
        };

        if AP {
            buckets.set_halflen(p_seq.len() as QPos);
        } else {
            buckets.set_halflen(m);
        }

        self.counters.inc("kmers_sketched", m as i64);
        self.counters.inc("kmers", m as i64);
        self.counters.inc("kmers_unique", p_unique.len() as i64);
        self.counters.inc("kmers_seeds", s as i64);
        self.timers.stop("prepare");

        self.timers.start("match_seeds");
        self.match_seeds(&p_unique, buckets, s);
        self.timers.stop("match_seeds");

        buckets.propagate_seeds_to_buckets();

        let mut sorted_buckets = buckets.get_sorted_buckets();
        self.counters.inc("seeded_buckets", sorted_buckets.len() as i64);

        if params.verbose >= 2 {
            eprintln!("kmers: {m} seeds: {s}");
            eprintln!(
                "seeded_buckets: {} total_matches: {}",
                self.counters.count("seeded_buckets"),
                self.counters.count("total_matches")
            );
            eprintln!(
                "B: bucket_halflen={} i={} seeds={}",
                buckets.halflen, buckets.i, buckets.seeds
            );
        }

        self.timers.start("match_rest");
        self.timers.start("match_rest_for_best");
        let best = self.match_rest(
            p_seq.len() as QPos,
            m,
            lmax,
            &p_unique,
            buckets,
            &mut sorted_buckets,
            &mut diff_hist,
            &p_ht,
            theta,
            None,
            params.verbose,
            params.max_overlap,
            params.metric,
            params.k,
        );
        self.timers.stop("match_rest_for_best");
        self.timers.start("match_rest_for_best2");
        let best2 = if let Some(best) = &best {
            let second_best_thr = best.score() * (1.0 - params.min_diff);
            self.match_rest(
                p_seq.len() as QPos,
                m,
                lmax,
                &p_unique,
                buckets,
                &mut sorted_buckets,
                &mut diff_hist,
                &p_ht,
                second_best_thr,
                Some(best),
                params.verbose,
                params.max_overlap,
                params.metric,
                params.k,
            )
        } else {
            None
        };
        self.timers.stop("match_rest_for_best2");
        self.timers.stop("match_rest");

        let fptp = -1.0; // ground-truth FDR calculation is dead upstream too (calc_FDR is never called)
        self.counters.inc("lost_on_pruning", 1); // always 1 upstream: `lost_on_pruning` is never actually recomputed from a real outcome (see match_rest)
        self.timers.stop("query_mapping");

        self.timers.start("output");
        let mut stdout = String::new();
        let mut unmapped_line = None;
        let mut paul_row = None;
        match best {
            Some(mut best) => {
                if let Some(best2) = &best2 {
                    best.set_second_best(best2);
                }

                self.counters.inc("matches_in_reported_mappings", best.intersection() as i64);
                self.counters.inc("J_best", (10000.0 * best.score()) as i64);
                self.counters.inc1("mappings");
                self.counters.inc1("mapped_reads");

                best.set_global_stats(
                    theta2,
                    params.min_diff,
                    m,
                    query_id,
                    p_seq.len() as QPos,
                    params.k,
                    s,
                    self.counters.count("total_matches") as i32,
                    self.counters.count("max_seed_matches") as i32,
                    self.counters.count("seed_matches") as i32,
                    self.counters.count("seeded_buckets") as i32,
                    self.counters.count("final_buckets") as i32,
                    fptp,
                    self.timers.secs("query_mapping"),
                );

                use std::fmt::Write as _;
                write!(stdout, "{best}").unwrap();
                if best.mapq() == 60 {
                    self.counters.inc1("mapq60");
                }
                if best.mapq() == 0 {
                    self.counters.inc1("mapq0");
                }

                if params.verbose >= 2 {
                    let mut gt = crate::analyse_simulated::AnalyseSimulatedReads::<AP>::new(
                        query_id,
                        p_seq,
                        p_seq.len() as QPos,
                        diff_hist.clone(),
                        m,
                        &p_ht,
                        self.tidx,
                        buckets,
                        params.theta,
                    );
                    let mut buf = Vec::new();
                    // `buf` is a `Vec<u8>`, whose `io::Write` impl cannot
                    // fail -- `.unwrap()`, not `?`, so a hypothetical future
                    // fallible sink here can't silently return early from
                    // between `self.timers.start("output")` (above) and
                    // `.stop("output")` (below) and leave that timer running
                    // forever inside this per-read `Timers` (see the
                    // `Timer::frozen`/`frozen_snapshot` fix this module
                    // already needed for a similar still-running-timer bug).
                    gt.print_paf(&mut buf).unwrap();
                    stdout.push_str(&String::from_utf8_lossy(&buf));
                    let gt_overlap = Mapping::overlap(&gt.gt_mapping, &best);
                    write!(
                        stdout,
                        "\tgt_mapping_len:i:{}\treported_mapping_len:i:{}\tgt_overlap:f:{:.3}",
                        gt.gt_mapping.paf.t_r - gt.gt_mapping.paf.t_l,
                        best.paf.t_r - best.paf.t_l,
                        gt_overlap
                    )
                    .unwrap();
                    paul_row = Some(gt.render_tsv_row());
                }
                stdout.push('\n');
            }
            None => {
                unmapped_line = Some(MappingPaf::unmapped_line(query_id, p_seq.len() as QPos));

                if params.verbose >= 2 {
                    let mut gt = crate::analyse_simulated::AnalyseSimulatedReads::<AP>::new(
                        query_id,
                        p_seq,
                        p_seq.len() as QPos,
                        diff_hist.clone(),
                        m,
                        &p_ht,
                        self.tidx,
                        buckets,
                        params.theta,
                    );
                    paul_row = Some(gt.render_tsv_row());
                }
            }
        }
        self.timers.stop("output");

        Ok(ReadOutput {
            stdout,
            unmapped_line,
            paul_row,
        })
    }
}

#[cfg(test)]
mod integration_tests {
    use super::*;
    use crate::handler::Handler;
    use crate::index::SketchIndex;
    use crate::params::Params;
    use clap::Parser;
    use std::io::Write;

    fn write_fasta(content: &str) -> tempfile::NamedTempFile {
        let mut f = tempfile::Builder::new().suffix(".fa").tempfile().unwrap();
        write!(f, "{content}").unwrap();
        f.flush().unwrap();
        f
    }

    fn run_variant<const NBP: bool, const OS: bool, const AP: bool>(ref_fa: &str, reads_fa: &str) {
        let ref_file = write_fasta(ref_fa);
        let reads_file = write_fasta(reads_fa);

        let params = Params::try_parse_from([
            "shmap",
            "-p",
            reads_file.path().to_str().unwrap(),
            "-s",
            ref_file.path().to_str().unwrap(),
            "-k",
            "8",
            "-r",
            "1.0",
            "-t",
            "0.1",
        ])
        .unwrap();
        params.validate().unwrap();

        let mut handler = Handler::new(params).unwrap();
        let mut tidx = SketchIndex::new();
        let profiler = Profiler::new(false);
        tidx.build_index(
            &handler.params.t_file.clone(),
            &handler.sketcher,
            handler.params.max_matches,
            &mut handler.counters,
            &mut handler.timers,
            &profiler,
            handler.params.threads,
        )
        .unwrap();

        let mut mapper: SHMapper<NBP, OS, AP> = SHMapper::new(&tidx);
        let p_file = handler.params.p_file.clone();
        mapper.map_reads(&mut handler, &p_file, &profiler).unwrap();
    }

    const REF: &str = ">ref\nACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT\n";
    const READS: &str = ">read1\nACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT\n>read2\nCGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT\n";

    #[test]
    fn all_eight_const_generic_combinations_run_without_panicking() {
        run_variant::<false, false, false>(REF, READS);
        run_variant::<true, false, false>(REF, READS);
        run_variant::<false, true, false>(REF, READS);
        run_variant::<false, false, true>(REF, READS);
        run_variant::<true, true, false>(REF, READS);
        run_variant::<true, false, true>(REF, READS);
        run_variant::<false, true, true>(REF, READS);
        run_variant::<true, true, true>(REF, READS);
    }

    /// Exercises the `verbose >= 2` ground-truth-comparison path wired up
    /// in `map_read` (`AnalyseSimulatedReads`, `.paul.tsv`, the
    /// `gt_*`/`gt_overlap` PAF tags), including for an unmapped read, with
    /// `params_file` set so both output files actually get created.
    #[test]
    fn verbose_ground_truth_path_runs_without_panicking() {
        let reference = ">chr1\nACGTGGCATTACGGATCCAGTGCATTGGACCTAGCATTGACCGGTAACCTTGGCATCGATGCCTAGGCATTACCGGATGCATCCGGTTACGATGCCATTGGACCTAGCATTGACCGGTA\n";
        // Ground-truth-encoded name (parsed by `ParsedQueryId`), an
        // unmapped-by-construction read (`nnnn...`, matches nothing), and
        // a real substring of chr1.
        let reads = ">sim1!chr1!10!50!+\nACGGATCCAGTGCATTGGACCTAGCATTGACCGGTAACCT\n>unmapped_sim!chr1!0!10!+\nNNNNNNNNNNNNNNNNNNNN\n";

        let ref_file = write_fasta(reference);
        let reads_file = write_fasta(reads);
        let params_prefix = format!("{}/shmap_test_params", std::env::temp_dir().display());

        let params = Params::try_parse_from([
            "shmap",
            "-p",
            reads_file.path().to_str().unwrap(),
            "-s",
            ref_file.path().to_str().unwrap(),
            "-k",
            "8",
            "-r",
            "1.0",
            "-t",
            "0.9",
            "-v",
            "2",
            "-z",
            &params_prefix,
        ])
        .unwrap();
        params.validate().unwrap();

        let mut handler = Handler::new(params).unwrap();
        let mut tidx = SketchIndex::new();
        let profiler = Profiler::new(true);
        tidx.build_index(
            &handler.params.t_file.clone(),
            &handler.sketcher,
            handler.params.max_matches,
            &mut handler.counters,
            &mut handler.timers,
            &profiler,
            handler.params.threads,
        )
        .unwrap();

        let mut mapper: SHMapper<false, false, false> = SHMapper::new(&tidx);
        let p_file = handler.params.p_file.clone();
        mapper.map_reads(&mut handler, &p_file, &profiler).unwrap();

        let paul_tsv = std::fs::read_to_string(format!("{params_prefix}.paul.tsv")).unwrap();
        assert!(paul_tsv.starts_with("query_id\t"));
        assert!(paul_tsv.contains("sim1!chr1!10!50!+"));
        assert!(paul_tsv.contains("unmapped_sim!chr1!0!10!+"));

        // With a strict theta, the mostly-N read shouldn't clear the
        // threshold against a real (N-free) reference.
        let unmapped_paf = std::fs::read_to_string(format!("{params_prefix}.unmapped.paf")).unwrap();
        assert!(unmapped_paf.contains("unmapped_sim!chr1!0!10!+"));

        let profile_path = format!("{params_prefix}.profile.json");
        profiler.finish_and_write(&profile_path, &handler.timers, &handler.counters).unwrap();
        let profile_json = std::fs::read_to_string(&profile_path).unwrap();
        assert!(profile_json.contains("\"threads\""));
        assert!(profile_json.contains("\"reader\""));
        assert!(profile_json.contains("\"worker-0\""));

        let _ = std::fs::remove_file(format!("{params_prefix}.paul.tsv"));
        let _ = std::fs::remove_file(format!("{params_prefix}.unmapped.paf"));
        let _ = std::fs::remove_file(&profile_path);
    }

    /// Regression test for a real deadlock: `-v 2` against reads whose names
    /// aren't ground-truth-encoded panics inside `map_read` by documented
    /// design (see `Params::verbose`'s doc comment). Before `catch_read_panic`
    /// was added, that panic killed the sole worker thread, which stopped it
    /// draining the bounded job channel; the reader thread then blocked
    /// forever trying to send into it, and `map_reads` blocked forever
    /// joining that reader — `-@ 1` (the default) hung permanently instead of
    /// erroring. Runs `map_reads` on a background thread and asserts it
    /// returns (with an `Err`, not a panic escaping) within a generous
    /// timeout, so a regression fails this test instead of hanging `cargo
    /// test` forever.
    #[test]
    fn a_panicking_read_errors_instead_of_hanging_the_pipeline() {
        let reference = ">chr1\nACGTGGCATTACGGATCCAGTGCATTGGACCTAGCATTGACCGGTAACCTTGGCATCGATGCCTAGGCATTACCGGATGCATCCGGTTACGATGCCATTGGACCTAGCATTGACCGGTA\n";
        // None of these names are ground-truth-encoded (no `!`-separated
        // fields), so every single one panics under `-v 2`.
        let reads = ">read0\nACGGATCCAGTGCATTGGACCTAGCATTGACCGGTAACCT\n\
                     >read1\nACGGATCCAGTGCATTGGACCTAGCATTGACCGGTAACCT\n\
                     >read2\nNNNNNNNNNNNNNNNNNNNN\n";

        let ref_file = write_fasta(reference);
        let reads_file = write_fasta(reads);

        let params = Params::try_parse_from([
            "shmap",
            "-p",
            reads_file.path().to_str().unwrap(),
            "-s",
            ref_file.path().to_str().unwrap(),
            "-k",
            "8",
            "-r",
            "1.0",
            "-t",
            "0.9",
            "-v",
            "2",
        ])
        .unwrap();
        params.validate().unwrap();

        let mut handler = Handler::new(params).unwrap();
        let mut tidx = SketchIndex::new();
        let profiler = Profiler::new(false);
        tidx.build_index(
            &handler.params.t_file.clone(),
            &handler.sketcher,
            handler.params.max_matches,
            &mut handler.counters,
            &mut handler.timers,
            &profiler,
            handler.params.threads,
        )
        .unwrap();

        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            // `tidx`/`handler`/`profiler` are moved in (not borrowed) so this
            // can be a plain 'static `thread::spawn` rather than a scoped
            // thread -- letting the test assert on a timeout via `rx`
            // without needing the spawning frame to outlive the spawned
            // thread the way `thread::scope` would require.
            let mut handler = handler;
            let mut mapper: SHMapper<false, false, false> = SHMapper::new(&tidx);
            let p_file = handler.params.p_file.clone();
            let result = mapper.map_reads(&mut handler, &p_file, &profiler);
            let _ = tx.send(result.is_err());
        });

        let returned_an_error = rx
            .recv_timeout(std::time::Duration::from_secs(30))
            .expect("map_reads hung instead of returning (deadlock regression)");
        assert!(returned_an_error, "a panicking read should make map_reads return Err, not Ok");
    }
}
