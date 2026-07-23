//! Reference k-mer index.
//!
//! Port of `shmap/src/index.h`.
//!
//! # Multithreaded sketching (`-@`/`--threads`)
//!
//! Not present upstream (indexing is entirely single-threaded there); added
//! because profiling (`PROFILING.md`) found reference indexing to be a fixed
//! serial floor that dominates whole-genome + few-reads workloads (~21s for
//! the full CHM13 genome, ~70% of total wall time on a 2000-read run against
//! it), the single biggest remaining lever once the `Buckets`-allocation fix
//! landed. `build_index` uses the same reader/worker-pool/collector pipeline
//! as `SHMapper::map_reads` (see that module's doc comment): one reader
//! thread streams segments off disk over a bounded channel, `threads`
//! worker threads sketch them in parallel (the actual FracMinHash k-mer
//! selection — independent per segment, so embarrassingly parallel), and the
//! scope's own thread collects completions and applies them
//! ([`SketchIndex::add_segment`]) strictly in original file order.
//!
//! That last part matters for determinism, not just style: `segm_id` is
//! assigned as `self.segments.len()` at the moment a segment is applied, and
//! `populate_h2pos`'s `max_matches` cap keeps only the first `m+1` hits it
//! sees for an over-frequent k-mer — both depend on *processing* order, not
//! just final content. Applying completed sketches in strict file order
//! (regardless of which order the worker threads actually finish sketching
//! them in) keeps both exactly matching the single-threaded result, the same
//! guarantee `map_reads` already provides for mapping output.

use std::collections::HashMap;
use std::sync::mpsc;
use std::sync::Mutex;

use rustc_hash::FxHashMap;

use crate::io::read_fasta;
use crate::profiling::Profiler;
use crate::sketch::{FracMinHash, RefSegment, SketchT};
use crate::types::{Hash, Hit, RPos, SegmId};
use crate::utils::{Counters, ProgressBar, Timers};

/// An indexed reference: k-mer sketches of every segment, plus a hash map
/// from k-mer hash to its hit(s) in the reference.
///
/// The C++ `SketchIndex` also stores a `Handler *H` back-pointer purely so
/// its methods can bump shared counters/timers; this port takes those as
/// explicit parameters instead (the same convention the C++ itself already
/// uses for e.g. `SHMapper::map_read`'s `params`/`sketcher` arguments),
/// which keeps `SketchIndex` plain, aliasing-free data.
#[derive(Default)]
pub struct SketchIndex {
    pub segments: Vec<RefSegment>,
    /// K-mers with exactly one hit in the reference.
    pub h2single: FxHashMap<Hash, Hit>,
    /// K-mers with more than one hit, each list sorted by `(segm_id, r)`
    /// (equivalently `(segm_id, tpos)`) to allow binary search.
    pub h2multi: FxHashMap<Hash, Vec<Hit>>,
}

impl SketchIndex {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn segments_len(&self) -> usize {
        self.segments.len()
    }

    pub fn get_segment_by_name(&self, name: &str) -> Option<&RefSegment> {
        self.segments.iter().find(|s| s.name == name)
    }

    pub fn get_segment(&self, segm_id: SegmId) -> &RefSegment {
        &self.segments[segm_id as usize]
    }

    /// Number of hits in the reference for k-mer hash `h`.
    pub fn count(&self, h: Hash) -> RPos {
        if self.h2single.contains_key(&h) {
            return 1;
        }
        if let Some(hits) = self.h2multi.get(&h) {
            return hits.len() as RPos;
        }
        0
    }

    fn populate_h2pos(&mut self, sketch: &SketchT, segm_id: SegmId, max_matches: Option<i32>) {
        for (tpos, kmer) in sketch.iter().enumerate() {
            let hit = Hit::new(kmer, tpos as RPos, segm_id);
            if !self.h2single.contains_key(&kmer.h) {
                self.h2single.insert(kmer.h, hit);
            } else {
                let multi = self.h2multi.entry(kmer.h).or_default();
                if max_matches.is_none_or(|m| (multi.len() as i32) < m + 1) {
                    multi.push(hit);
                }
            }
        }
    }

    fn add_segment(
        &mut self,
        segm_name: String,
        segm_sz: RPos,
        sketch: SketchT,
        max_matches: Option<i32>,
        counters: &mut Counters,
    ) {
        let segm_id = self.segments.len() as SegmId;
        counters.inc1("segments");
        counters.inc("total_nucls", segm_sz as i64);
        // Populate from `sketch` (a local, not-yet-stored value) before
        // moving it into `self.segments`, so this doesn't need to borrow
        // `self.segments` immutably while `populate_h2pos` borrows `self`
        // mutably.
        self.populate_h2pos(&sketch, segm_id, max_matches);
        self.segments
            .push(RefSegment::new(sketch, segm_name, segm_sz, segm_id));
    }

    fn get_kmer_stats(&self, counters: &mut Counters) {
        let mut max_occ: RPos = 0;
        counters.inc("indexed_hits", self.h2single.len() as i64);
        counters.inc("indexed_kmers", self.h2single.len() as i64);
        for hits in self.h2multi.values() {
            let occ = hits.len() as RPos;
            counters.inc("indexed_hits", occ as i64);
            counters.inc1("indexed_kmers");
            if occ > max_occ {
                max_occ = occ;
            }
        }
        counters.inc("indexed_highest_freq_kmer", max_occ as i64);
    }

    fn erase_frequent_kmers(&mut self, max_matches: i32, counters: &mut Counters) {
        let blacklisted: Vec<Hash> = self
            .h2multi
            .iter()
            .filter(|(_, hits)| hits.len() as i32 > max_matches)
            .map(|(h, hits)| {
                counters.inc1("blacklisted_kmers");
                counters.inc("blacklisted_hits", hits.len() as i64);
                *h
            })
            .collect();
        for h in blacklisted {
            self.h2multi.remove(&h);
        }
    }

    /// Reads `t_file`, sketches each segment, and populates the index.
    /// `threads` (`params.threads`, the same knob `-@` uses for mapping)
    /// parallelizes the sketching step across segments — see the module
    /// doc comment for the pipeline shape and why it's still
    /// thread-count-independent/deterministic.
    #[allow(clippy::too_many_arguments)]
    pub fn build_index(
        &mut self,
        t_file: &str,
        sketcher: &FracMinHash,
        max_matches: Option<i32>,
        counters: &mut Counters,
        timers: &mut Timers,
        profiler: &Profiler,
        threads: usize,
    ) -> anyhow::Result<()> {
        let progress_bar = ProgressBar::new("Indexing");

        // Pre-register so `print_stats` can report 0 rather than panic if
        // the reference file turns out to have zero segments.
        counters.init(&["segments", "total_nucls"]);

        timers.start("indexing");
        eprintln!("Indexing {t_file}...");

        let n_threads = threads.max(1);

        struct SegJob {
            idx: u64,
            segm_name: String,
            seq: Vec<u8>,
            progress: f32,
        }
        struct SegDone {
            idx: u64,
            segm_name: String,
            seq_len: RPos,
            sketch: SketchT,
            progress: f32,
            counters: Counters,
            timers: Timers,
        }

        // Bounded for the same reason as `map_reads`'s job channel: caps how
        // far the reader can get ahead of the sketching workers.
        let (job_tx, job_rx) = mpsc::sync_channel::<SegJob>(n_threads * 4);
        let job_rx = Mutex::new(job_rx);
        let (done_tx, done_rx) = mpsc::channel::<SegDone>();

        std::thread::scope(|scope| -> anyhow::Result<()> {
            for worker_idx in 0..n_threads {
                let job_rx = &job_rx;
                let done_tx = done_tx.clone();
                scope.spawn(move || {
                    let mut thread_timers = Timers::new();
                    let mut thread_counters = Counters::new();
                    let mut jobs_done: u64 = 0;
                    loop {
                        let job = job_rx.lock().unwrap().recv();
                        let Ok(job) = job else { break };
                        let mut seg_counters = Counters::new();
                        let mut seg_timers = Timers::new();
                        seg_timers.start("index_sketching");
                        let sketch = sketcher.sketch(&job.seq, &mut seg_counters);
                        seg_timers.stop("index_sketching");
                        if profiler.enabled() {
                            thread_timers += &seg_timers;
                            thread_counters += &seg_counters;
                            jobs_done += 1;
                        }
                        let done = SegDone {
                            idx: job.idx,
                            segm_name: job.segm_name,
                            seq_len: job.seq.len() as RPos,
                            sketch,
                            progress: job.progress,
                            counters: seg_counters,
                            timers: seg_timers,
                        };
                        if done_tx.send(done).is_err() {
                            break;
                        }
                    }
                    if profiler.enabled() {
                        profiler.record_thread(
                            format!("index-worker-{worker_idx}"),
                            "index_sketch",
                            jobs_done,
                            thread_timers,
                            thread_counters,
                        );
                    }
                });
            }
            drop(done_tx);

            let reader = scope.spawn(move || -> anyhow::Result<Timers> {
                let mut r_timers = Timers::new();
                r_timers.init(&["index_reading"]);
                r_timers.start("index_reading");
                let mut idx = 0u64;
                read_fasta(t_file, |segm_name, seq, progress| {
                    r_timers.stop("index_reading");
                    let _ = job_tx.send(SegJob {
                        idx,
                        segm_name: segm_name.to_string(),
                        seq: seq.to_vec(),
                        progress,
                    });
                    idx += 1;
                    r_timers.start("index_reading");
                })?;
                r_timers.stop("index_reading");
                Ok(r_timers)
            });

            // Applies each segment's already-computed sketch strictly in
            // original file order (never in whatever order workers actually
            // finish sketching) — see the module doc comment for why this
            // is required for determinism, not just for a stable progress
            // bar.
            let mut next_idx = 0u64;
            let mut pending: HashMap<u64, SegDone> = HashMap::new();
            while let Ok(done) = done_rx.recv() {
                pending.insert(done.idx, done);
                while let Some(done) = pending.remove(&next_idx) {
                    *counters += &done.counters;
                    timers.start("index_initializing");
                    self.add_segment(done.segm_name, done.seq_len, done.sketch, max_matches, counters);
                    timers.stop("index_initializing");
                    *timers += &done.timers;
                    progress_bar.update(done.progress as f64);
                    next_idx += 1;
                }
            }

            let reader_timers = reader.join().expect("index reader thread panicked")?;
            *timers += &reader_timers;
            Ok(())
        })?;
        eprintln!();

        // Migrate any k-mer that ended up in both `h2single` and `h2multi`
        // (its second occurrence was discovered after the first was
        // already placed in `h2single`) fully into `h2multi`, then sort
        // each multi-hit list by `(segm_id, r)` to allow binary search.
        for (h, hits) in self.h2multi.iter_mut() {
            if let Some(single_hit) = self.h2single.remove(h) {
                hits.push(single_hit);
            }
            hits.sort_by(|a, b| a.segm_id.cmp(&b.segm_id).then(a.r.cmp(&b.r)));
        }
        timers.stop("indexing");

        self.get_kmer_stats(counters);
        counters.inc("blacklisted_kmers", 0);
        counters.inc("blacklisted_hits", 0);
        if let Some(max_matches) = max_matches {
            self.erase_frequent_kmers(max_matches, counters);
        }
        self.print_stats(sketcher.k, counters, timers);

        if profiler.enabled() {
            // `frozen_snapshot`, not a plain `.clone()`: `timers` is
            // `handler.timers`, whose run-wide "total" entry is still
            // running here (it only stops on `Handler`'s `Drop`, well after
            // mapping finishes) — a naive clone would keep advancing with
            // the wall clock by the time this gets serialized at the very
            // end of the run, reporting the whole program's wall time
            // instead of "how long had elapsed when indexing finished".
            profiler.record_thread(
                "indexer",
                "index",
                self.segments.len() as u64,
                timers.frozen_snapshot(),
                counters.clone(),
            );
        }
        Ok(())
    }

    fn print_stats(&self, k: i32, counters: &Counters, timers: &Timers) {
        eprintln!(" | total nucleotides:     {}", counters.count("total_nucls"));
        eprintln!(
            " | index segments:        {} (~{:.1} per segment)",
            counters.count("segments"),
            counters.count("total_nucls") as f64 / counters.count("segments") as f64
        );
        for segm in &self.segments {
            eprintln!(" | | {} ({} nb)", segm.name, segm.sz);
        }
        eprintln!(" | indexed kmers:         {}", counters.count("indexed_kmers"));
        eprintln!(
            " | indexed hits:          {} ({:.1}% of the index, ~{:.1} per kmer)",
            counters.count("indexed_hits"),
            k as f64 * counters.perc("indexed_hits", "total_nucls"),
            counters.frac("indexed_hits", "indexed_kmers")
        );
        eprintln!(
            " | | most frequent kmer:      {} times.",
            counters.count("indexed_highest_freq_kmer")
        );
        eprintln!(
            " | | blacklisted kmers:       {} ({:.1}%)",
            counters.count("blacklisted_kmers"),
            counters.perc("blacklisted_kmers", "indexed_kmers")
        );
        eprintln!(
            " | | blacklisted hits:        {} ({:.1}%)",
            counters.count("blacklisted_hits"),
            counters.perc("blacklisted_hits", "indexed_hits")
        );
        eprintln!(" | indexing time:        {:.1}s", timers.secs("indexing"));
        eprintln!(" | | reading time:          {:.1}", timers.secs("index_reading"));
        eprintln!(" | | sketching time:        {:.1}", timers.secs("index_sketching"));
        eprintln!(" | | initializing time:     {:.1}", timers.secs("index_initializing"));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn fasta_file(content: &str) -> tempfile::NamedTempFile {
        let mut f = tempfile::Builder::new().suffix(".fa").tempfile().unwrap();
        write!(f, "{content}").unwrap();
        f.flush().unwrap();
        f
    }

    #[test]
    fn single_segment_indexing_matches_expected_hit_counts() {
        let f = fasta_file(">ref\nACCAGTACCA\n");
        let sketcher = FracMinHash::new(4, 1.0);
        let mut counters = Counters::new();
        let mut timers = Timers::new();
        let mut tidx = SketchIndex::new();
        tidx.build_index(
            f.path().to_str().unwrap(),
            &sketcher,
            None,
            &mut counters,
            &mut timers,
            &Profiler::new(false),
            1,
        )
        .unwrap();

        let t = sketcher.sketch(b"ACCAGTACCA", &mut Counters::new());
        assert_eq!(t.len(), 7);
        let expected = [2, 1, 1, 1, 1, 1, 2];
        for (kmer, &want) in t.iter().zip(expected.iter()) {
            assert_eq!(tidx.count(kmer.h), want);
        }
        assert_eq!(counters.count("indexed_hits"), 7);
        assert_eq!(counters.count("indexed_kmers"), 6);
    }

    #[test]
    fn two_segments_share_kmer_counts_across_both() {
        let f = fasta_file(">segm1\nACCAGTACCA\n>segm2\nGGACCA\n");
        let sketcher = FracMinHash::new(4, 1.0);
        let mut counters = Counters::new();
        let mut timers = Timers::new();
        let mut tidx = SketchIndex::new();
        tidx.build_index(
            f.path().to_str().unwrap(),
            &sketcher,
            None,
            &mut counters,
            &mut timers,
            &Profiler::new(false),
            1,
        )
        .unwrap();

        let t1 = sketcher.sketch(b"ACCAGTACCA", &mut Counters::new());
        assert_eq!(t1.len(), 7);
        let expected = [3, 1, 1, 1, 1, 1, 3];
        for (kmer, &want) in t1.iter().zip(expected.iter()) {
            assert_eq!(tidx.count(kmer.h), want);
        }
        assert_eq!(counters.count("indexed_hits"), 10);
        assert_eq!(counters.count("indexed_kmers"), 8);
    }

    /// Regression test for the determinism `build_index`'s module doc
    /// comment claims: segments are assigned `segm_id`s by file order and
    /// `max_matches` caps the *first* `m+1` hits seen for an over-frequent
    /// k-mer, both of which depend on processing order, not just final
    /// content -- so building the same reference at `-@ 1` vs `-@ 8` must
    /// still apply completed sketches in strict file order rather than
    /// whatever order the worker threads happen to finish sketching in.
    /// Many segments share a common prefix (so plenty of k-mers land in
    /// `h2multi` across segment boundaries) with a small `max_matches` (so
    /// the order-sensitive cap actually triggers), specifically to give a
    /// wrong merge order a real chance to produce a different index.
    #[test]
    fn multithreaded_indexing_matches_single_threaded_indexing() {
        let repeated = "ACGTACGTACGTACGTACGT";
        let mut content = String::new();
        for i in 0..10 {
            content.push_str(&format!(">segm{i}\n{repeated}TTTTGGGGCCCCAAAA{i}\n"));
        }
        let f = fasta_file(&content);
        let sketcher = FracMinHash::new(6, 1.0);
        let max_matches = Some(3);

        let mut counters1 = Counters::new();
        let mut timers1 = Timers::new();
        let mut tidx1 = SketchIndex::new();
        tidx1
            .build_index(
                f.path().to_str().unwrap(),
                &sketcher,
                max_matches,
                &mut counters1,
                &mut timers1,
                &Profiler::new(false),
                1,
            )
            .unwrap();

        let mut counters8 = Counters::new();
        let mut timers8 = Timers::new();
        let mut tidx8 = SketchIndex::new();
        tidx8
            .build_index(
                f.path().to_str().unwrap(),
                &sketcher,
                max_matches,
                &mut counters8,
                &mut timers8,
                &Profiler::new(false),
                8,
            )
            .unwrap();

        // Sanity: the shared prefix actually produced an over-frequent k-mer
        // for `max_matches` to blacklist, i.e. this test is actually
        // exercising the order-sensitive path and not vacuously passing.
        assert!(counters1.count("blacklisted_kmers") > 0);

        assert_eq!(
            tidx1.segments.iter().map(|s| (s.name.clone(), s.sz)).collect::<Vec<_>>(),
            tidx8.segments.iter().map(|s| (s.name.clone(), s.sz)).collect::<Vec<_>>(),
            "segm_id assignment (file order) diverged between thread counts"
        );
        assert_eq!(tidx1.h2single, tidx8.h2single, "h2single diverged between thread counts");
        assert_eq!(tidx1.h2multi, tidx8.h2multi, "h2multi diverged between thread counts");
        assert_eq!(counters1.count("indexed_hits"), counters8.count("indexed_hits"));
        assert_eq!(counters1.count("indexed_kmers"), counters8.count("indexed_kmers"));
        assert_eq!(counters1.count("blacklisted_kmers"), counters8.count("blacklisted_kmers"));
    }
}
