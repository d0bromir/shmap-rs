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
//! worker threads sketch in parallel, and the scope's own thread collects
//! completions and applies them ([`SketchIndex::add_segment`]) strictly in
//! original file order.
//!
//! The unit of work is a *chunk* of one segment, not a whole segment. Handing
//! each worker an entire chromosome caps the whole phase at the time to sketch
//! the single longest one — on a human reference that is ~8% of the bases in
//! one indivisible piece, so `-@ 16` indexed barely faster than `-@ 1`. A
//! k-mer window depends only on the `k` bases under it, so a segment splits
//! into runs of windows that can be sketched independently and concatenated
//! (see [`FracMinHash::sketch_slice_into`], and `chunk_windows` for how the
//! split is sized). Chunks of one segment share a single `Arc`'d copy of its
//! bases, which also bounds resident sequence: the old form let the reader
//! buffer `threads * 4` whole chromosomes as read-ahead.
//!
//! Applying in file order matters for determinism, not just style: `segm_id`
//! is assigned as `self.segments.len()` at the moment a segment is applied,
//! and `populate_h2pos`'s `max_matches` cap keeps only the first `m+1` hits
//! it sees for an over-frequent k-mer — both depend on *processing* order,
//! not just final content. Reassembling each segment's chunks by index and
//! applying whole segments in strict file order (regardless of the order
//! workers actually finish in) keeps both exactly matching the
//! single-threaded result, the same guarantee `map_reads` already provides
//! for mapping output.

use std::collections::HashMap;
use std::sync::mpsc;
use std::sync::Arc;
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

/// How many k-mer windows one sketching chunk should cover.
///
/// Sketching a segment is split into chunks so that a single long chromosome
/// cannot pin one worker while every other thread idles — the Amdahl cap that
/// made `-@ 16` barely beat `-@ 1` on indexing-dominated runs. Chunks are
/// sized to give each worker several pieces (so a straggler costs a fraction
/// of a chunk, not a fraction of a chromosome) while staying large enough
/// that the `k - 1` bases each chunk re-reads at its left edge, and its
/// separate output `Vec`, stay negligible.
fn chunk_windows(n_windows: usize, n_threads: usize) -> usize {
    // With a single worker, chunking buys no parallelism and would only add
    // the overlap and a concatenation pass, so keep whole segments.
    if n_threads <= 1 {
        return n_windows.max(1);
    }
    const MIN_CHUNK: usize = 1 << 21;
    (n_windows / (n_threads * 4)).max(MIN_CHUNK)
}

/// Joins a segment's per-chunk sketches back into the single sketch a
/// whole-segment sketching pass would have produced.
fn concat_chunks(mut chunks: Vec<Option<SketchT>>) -> SketchT {
    // The overwhelmingly common case (small segment, or `-@ 1`): hand the one
    // chunk's buffer straight through rather than copying it.
    if chunks.len() == 1 {
        return chunks.pop().flatten().unwrap_or_default();
    }
    let total: usize = chunks.iter().flatten().map(|s| s.len()).sum();
    let mut out = Vec::with_capacity(total);
    for chunk in chunks.into_iter().flatten() {
        out.extend_from_slice(&chunk);
    }
    out
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
            // `entry` instead of `contains_key` + `insert`/`entry`: hashes
            // `kmer.h` once instead of twice for the common (single-hit)
            // case — this loop runs once per indexed k-mer (~19M for the
            // full CHM13 genome), so the redundant hash was a real,
            // measurable cost in `index_initializing`.
            match self.h2single.entry(kmer.h) {
                std::collections::hash_map::Entry::Vacant(e) => {
                    e.insert(hit);
                }
                std::collections::hash_map::Entry::Occupied(_) => {
                    // `with_capacity(2)`, not `or_default()`: a k-mer that
                    // reaches `h2multi` at all ends up holding at least two
                    // hits (this one, plus the `h2single` entry that the
                    // migration pass at the end of `build_index` pushes in),
                    // and most hold exactly two. Starting empty made that
                    // near-universal case allocate at capacity 1 and then
                    // immediately reallocate to 2, once per repeated k-mer —
                    // millions of avoidable reallocations on a whole genome,
                    // for the same final memory.
                    let multi = self.h2multi.entry(kmer.h).or_insert_with(|| Vec::with_capacity(2));
                    if max_matches.is_none_or(|m| (multi.len() as i32) < m + 1) {
                        multi.push(hit);
                    }
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

        // NB: an up-front `h2single.reserve(file_bytes * h_frac)` was tried
        // and *regressed* indexing — `file_bytes * h_frac` over-estimates
        // the true distinct-k-mer count (repeats mean far fewer distinct
        // k-mers than total), so the reserve built a large, sparse table
        // whose cold random-access inserts cost more cache misses than the
        // default doubling growth's periodic (but cache-warm) rehashes, and
        // it inflated peak RSS too. Default growth wins here; left un-reserved.

        let n_threads = threads.max(1);
        let k = sketcher.k;

        /// One contiguous run of k-mer windows of one segment, to be sketched
        /// by whichever worker picks it up. `[w0, w1)` indexes *windows*, not
        /// bases: window `w` is the k-mer ending at base `w + k - 1`, so the
        /// bases this job actually reads are `seq[w0..w1 + k - 1]`.
        struct ChunkJob {
            /// Shared with this segment's other chunks, and deliberately *not*
            /// carried into `ChunkDone`: the last worker to finish a chunk
            /// drops the last handle, so a segment's bases are freed as soon
            /// as it is sketched rather than being held alive across the much
            /// slower `add_segment` that follows.
            seq: Arc<Vec<u8>>,
            meta: SegMeta,
            chunk_idx: u32,
            w0: usize,
            w1: usize,
        }
        /// The small, sequence-free per-segment facts the collector needs.
        #[derive(Clone)]
        struct SegMeta {
            idx: u64,
            name: Arc<str>,
            seq_len: RPos,
            n_chunks: u32,
            progress: f32,
        }
        struct ChunkDone {
            meta: SegMeta,
            chunk_idx: u32,
            sketch: SketchT,
            timers: Timers,
        }
        /// A segment's chunk sketches as they arrive, plus how many are still
        /// outstanding.
        struct SegAssembly {
            meta: SegMeta,
            chunks: Vec<Option<SketchT>>,
            remaining: u32,
        }

        // Bounded for the same reason as `map_reads`'s job channel: caps how
        // far the reader can get ahead of the sketching workers. Because a
        // segment's chunks all borrow one shared `Arc<Vec<u8>>`, this also
        // bounds how many whole segments can be resident at once — the old
        // one-job-per-segment form let the reader buffer `n_threads * 4`
        // entire chromosomes, which on a multi-Gbp reference was gigabytes of
        // sequence held purely as read-ahead.
        let (job_tx, job_rx) = mpsc::sync_channel::<ChunkJob>(n_threads * 4);
        let job_rx = Mutex::new(job_rx);
        let (done_tx, done_rx) = mpsc::channel::<ChunkDone>();

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
                        let mut seg_timers = Timers::new();
                        seg_timers.start("index_sketching");
                        // Windows `[w0, w1)` are the k-mers ending at bases
                        // `[w0 + k - 1, w1 + k - 2]`, so they read exactly
                        // these bases. `w0` is passed as the offset so the
                        // k-mer positions come out absolute in the segment.
                        let bases = &job.seq[job.w0..(job.w1 + (k - 1).max(0) as usize).min(job.seq.len())];
                        let n_bases = bases.len();
                        let sketch = sketcher.sketch_slice_into(bases, job.w0 as RPos, Vec::new());
                        seg_timers.stop("index_sketching");
                        drop(job.seq);
                        if profiler.enabled() {
                            thread_timers += &seg_timers;
                            thread_counters.inc("sketched_len", n_bases as i64);
                            jobs_done += 1;
                        }
                        let done = ChunkDone {
                            meta: job.meta,
                            chunk_idx: job.chunk_idx,
                            sketch,
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
                // Separate object: `read_fasta`'s own `&mut Timers` argument
                // would otherwise alias the callback's mutable capture of
                // `r_timers` below. Merged in once `read_fasta` returns.
                let mut fasta_timers = Timers::new();
                r_timers.start("index_reading");
                let mut idx = 0u64;
                read_fasta(t_file, &mut fasta_timers, |segm_name, seq, progress| {
                    r_timers.stop("index_reading");
                    let n_windows = seq.len().saturating_sub((k - 1).max(0) as usize);
                    let per_chunk = chunk_windows(n_windows, n_threads);
                    let n_chunks = n_windows.div_ceil(per_chunk).max(1) as u32;
                    let meta = SegMeta {
                        idx,
                        name: Arc::from(segm_name),
                        seq_len: seq.len() as RPos,
                        n_chunks,
                        progress,
                    };
                    let seq = Arc::new(seq);
                    for chunk_idx in 0..n_chunks {
                        let w0 = chunk_idx as usize * per_chunk;
                        let w1 = ((chunk_idx as usize + 1) * per_chunk).min(n_windows);
                        let job = ChunkJob {
                            seq: Arc::clone(&seq),
                            meta: meta.clone(),
                            chunk_idx,
                            w0,
                            w1,
                        };
                        if job_tx.send(job).is_err() {
                            break;
                        }
                    }
                    // The reader's own handle goes away here; the last worker
                    // to finish one of these chunks frees the bases.
                    drop(seq);
                    idx += 1;
                    r_timers.start("index_reading");
                })?;
                r_timers.stop("index_reading");
                r_timers += &fasta_timers;
                Ok(r_timers)
            });

            // Reassembles each segment from its chunk sketches and applies it
            // strictly in original file order (never in whatever order
            // workers actually finish sketching) — see the module doc comment
            // for why this is required for determinism, not just for a stable
            // progress bar.
            let mut next_idx = 0u64;
            let mut pending: HashMap<u64, SegAssembly> = HashMap::new();
            while let Ok(done) = done_rx.recv() {
                *timers += &done.timers;
                let n_chunks = done.meta.n_chunks;
                let asm = pending.entry(done.meta.idx).or_insert_with(|| SegAssembly {
                    meta: done.meta.clone(),
                    chunks: (0..n_chunks).map(|_| None).collect(),
                    remaining: n_chunks,
                });
                if asm.chunks[done.chunk_idx as usize].replace(done.sketch).is_none() {
                    asm.remaining -= 1;
                }

                while pending.get(&next_idx).is_some_and(|a| a.remaining == 0) {
                    let asm = pending.remove(&next_idx).expect("just checked it is present");
                    let seq_len = asm.meta.seq_len;
                    let sketch = concat_chunks(asm.chunks);

                    // These are the counters `FracMinHash::sketch` bumps when
                    // a sequence is sketched in one piece; bumped here, once
                    // per segment, so a segment split across N chunks still
                    // reports as exactly one sketched sequence.
                    counters.inc1("sketched_seqs");
                    counters.inc("sketched_len", seq_len as i64);
                    counters.inc("original_kmers", sketch.len() as i64);
                    counters.inc("sketched_kmers", sketch.len() as i64);

                    timers.start("index_initializing");
                    self.add_segment(asm.meta.name.to_string(), seq_len, sketch, max_matches, counters);
                    timers.stop("index_initializing");
                    progress_bar.update(asm.meta.progress as f64);
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
            // These lists are read-only for the rest of the run, so the slack
            // left by doubling growth is dead weight held for the whole
            // mapping phase — but reclaiming it costs a reallocation and a
            // copy, so only do it where the slack is actually worth it. The
            // count is dominated by two- and three-hit lists whose slack is a
            // single `Hit`; shrinking those was ~5.8M reallocations for ~100 MB
            // on a k=25 whole-genome index, and measured as a net loss. The
            // bytes are dominated by the rare very-high-frequency k-mers,
            // whose lists are big enough that this test passes easily.
            let slack = hits.capacity() - hits.len();
            if slack > hits.len() / 8 + 8 {
                hits.shrink_to_fit();
            }
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
