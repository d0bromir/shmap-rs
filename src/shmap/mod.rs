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

/// Applies one read's already-rendered [`ReadOutput`] — the only place that
/// actually touches stdout/the unmapped-PAF file/`paul.tsv` during mapping.
/// Must be called strictly in original read order for output to match a
/// single-threaded run.
fn apply_read_output(
    output: &ReadOutput,
    unmapped_out: Option<&mut std::fs::File>,
    paulout: Option<&mut std::fs::File>,
    paulout_is_first_row: &mut bool,
) -> anyhow::Result<()> {
    if !output.stdout.is_empty() {
        print!("{}", output.stdout);
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
    pub fn map_reads(&mut self, handler: &mut Handler, p_file: &str) -> anyhow::Result<()> {
        handler.counters.init(&["reads"]);
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

        handler.timers.start("mapping");

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
            for _ in 0..n_threads {
                let job_rx = &job_rx;
                let done_tx = done_tx.clone();
                scope.spawn(move || {
                    let mut worker: SHMapper<'_, NBP, OS, AP> = SHMapper::new(tidx);
                    let mut buckets: Buckets<'_, AP> = Buckets::new(tidx);
                    loop {
                        let job = job_rx.lock().unwrap().recv();
                        let Ok(job) = job else { break };
                        let result = worker.map_read(sketcher, params, &job.query_id, &job.seq, &mut buckets);
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
                Ok(timers)
            });

            let mut next_idx = 0u64;
            let mut pending: HashMap<u64, Done> = HashMap::new();
            while let Ok(done) = done_rx.recv() {
                pending.insert(done.idx, done);
                while let Some(done) = pending.remove(&next_idx) {
                    handler.counters.inc1("reads");
                    handler.counters += &done.counters;
                    handler.timers += &done.timers;

                    match done.result {
                        Ok(output) => {
                            apply_read_output(&output, unmapped_out.as_mut(), paulout.as_mut(), &mut paulout_is_first_row)?;
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
                }
            }

            let reader_timers = reader.join().expect("reader thread panicked")?;
            handler.timers += &reader_timers;
            Ok(())
        })?;
        if let Some(e) = read_err {
            return Err(e);
        }

        handler.timers.stop("mapping");
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
                    gt.print_paf(&mut buf)?;
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
        tidx.build_index(
            &handler.params.t_file.clone(),
            &handler.sketcher,
            handler.params.max_matches,
            &mut handler.counters,
            &mut handler.timers,
        )
        .unwrap();

        let mut mapper: SHMapper<NBP, OS, AP> = SHMapper::new(&tidx);
        let p_file = handler.params.p_file.clone();
        mapper.map_reads(&mut handler, &p_file).unwrap();
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
        tidx.build_index(
            &handler.params.t_file.clone(),
            &handler.sketcher,
            handler.params.max_matches,
            &mut handler.counters,
            &mut handler.timers,
        )
        .unwrap();

        let mut mapper: SHMapper<false, false, false> = SHMapper::new(&tidx);
        let p_file = handler.params.p_file.clone();
        mapper.map_reads(&mut handler, &p_file).unwrap();

        let paul_tsv = std::fs::read_to_string(format!("{params_prefix}.paul.tsv")).unwrap();
        assert!(paul_tsv.starts_with("query_id\t"));
        assert!(paul_tsv.contains("sim1!chr1!10!50!+"));
        assert!(paul_tsv.contains("unmapped_sim!chr1!0!10!+"));

        // With a strict theta, the mostly-N read shouldn't clear the
        // threshold against a real (N-free) reference.
        let unmapped_paf = std::fs::read_to_string(format!("{params_prefix}.unmapped.paf")).unwrap();
        assert!(unmapped_paf.contains("unmapped_sim!chr1!0!10!+"));

        let _ = std::fs::remove_file(format!("{params_prefix}.paul.tsv"));
        let _ = std::fs::remove_file(format!("{params_prefix}.unmapped.paf"));
    }
}
