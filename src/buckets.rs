//! Position-bucketed candidate-region accumulators.
//!
//! Port of `shmap/src/buckets.h`.
//!
//! `AP` (the C++ `abs_pos` template bool) selects whether bucket indices
//! are computed from a hit's absolute reference position (`hit.r`) or its
//! index into the reference sketch (`hit.tpos`).

use rustc_hash::FxHashMap;

use crate::index::SketchIndex;
use crate::types::{BucketContent, BucketLoc, Hit, QPos, RPos};

/// Smallest allowed bucket half-length.
pub const MIN_HALFLEN: QPos = 5;

/// Bits per digit in [`Buckets`]'s LSD radix sort — chosen so the counting
/// histogram (`RADIX_SIZE` `u32`s) comfortably fits in L2 cache. Smaller
/// digits (more passes) measured *worse* in practice despite the smaller
/// histogram: each pass's scatter step is semi-random-access regardless of
/// digit width, so halving the digit size (doubling the pass count) simply
/// doubles the total data movement without a matching cache-locality win.
const RADIX_BITS: u32 = 16;
const RADIX_SIZE: usize = 1 << RADIX_BITS;
const RADIX_MASK: u64 = (RADIX_SIZE as u64) - 1;
/// A packed `(segm_id, b)` key is 64 bits; this many `RADIX_BITS`-wide
/// passes cover the worst case. `radix_sort_entries` computes how many are
/// actually needed per call — `segm_id` is a handful of segments and `b` is
/// a reference position divided by a read-scaled `halflen`, so most reads
/// need far fewer than the full width.
const RADIX_MAX_PASSES: u32 = 64 / RADIX_BITS;

/// Packs a [`BucketLoc`] into a 64-bit key (`segm_id` in the high 32 bits,
/// `b` in the low 32 bits) that sorts in the same order as comparing
/// `(segm_id, b)` lexicographically — both fields are always non-negative
/// (segment indices and `pos / halflen` bucket indices), so the `i32 -> u32`
/// bit-reinterpretation preserves ordering.
fn radix_key(loc: &BucketLoc) -> u64 {
    debug_assert!(loc.segm_id >= 0 && loc.b >= 0);
    ((loc.segm_id as u32 as u64) << 32) | (loc.b as u32 as u64)
}

/// Bucket accumulator storage backed by a hashmap, keyed by `BucketLoc`.
///
/// Upstream, this is used only as ephemeral per-seed scratch space inside
/// `match_seeds` (to de-duplicate one seed's own multi-hits before merging
/// them into the main `Buckets` store) — it is *not* a swappable
/// alternative backend for the mapper's primary bucket storage (that's
/// always `Buckets`). Ported with only the methods that narrower role
/// actually exercises: `get_sorted_buckets`/`size` have zero call sites on
/// this type in the C++ (confirmed via grep) and are dropped, matching how
/// `Buckets::size()` — a stub that literally returns `-1` — is dropped too.
pub struct BucketsHash<const AP: bool> {
    pub halflen: QPos,
    pub buckets: FxHashMap<BucketLoc, BucketContent>,
}

impl<const AP: bool> BucketsHash<AP> {
    pub fn new(halflen: QPos) -> Self {
        BucketsHash {
            halflen,
            buckets: FxHashMap::default(),
        }
    }

    pub fn begin(&self, b: &BucketLoc) -> RPos {
        b.b * self.halflen
    }

    pub fn end(&self, b: &BucketLoc) -> RPos {
        (b.b + 2) * self.halflen
    }

    /// Adds `content` to the bucket containing `hit`, and to the preceding
    /// bucket too (buckets overlap: bucket `b` spans `[b, b+2)` half-lengths,
    /// so a hit in half-length `b` also falls inside bucket `b-1`).
    pub fn add_to_pos(&mut self, hit: &Hit, content: BucketContent) {
        let b = (if AP { hit.r } else { hit.tpos }) / self.halflen;
        *self.buckets.entry(BucketLoc::new(hit.segm_id, b)).or_default() += content;
        if b > 0 {
            *self
                .buckets
                .entry(BucketLoc::new(hit.segm_id, b - 1))
                .or_default() += content;
        }
    }

    /// Empties the map for reuse as the next seed's scratch space, keeping
    /// its already-allocated capacity — lets a single `BucketsHash` be
    /// reused across every multi-hit seed in a read (`match_seeds`) instead
    /// of allocating a fresh one per seed.
    pub fn clear(&mut self) {
        self.buckets.clear();
    }
}

/// The mapper's primary bucket storage, keyed by `BucketLoc` (segment +
/// `tpos / halflen`, or `r / halflen` when `AP`) rather than a flat,
/// reference-sized array.
///
/// This used to be one dense `Vec<BucketContent>` per reference segment,
/// sized up front from the segment's length (`sz / MIN_HALFLEN + 2` slots) —
/// for a multi-Gbp genome that's a ~15 GB allocation *per worker thread*,
/// re-zeroed on every `clear()`-tracked touch but otherwise sitting almost
/// entirely idle (a read only ever touches a handful of buckets near where
/// it maps). Profiling that one-time allocation+zero-init (see
/// `PROFILING.md`) found it costs 7-21+ seconds per worker depending on how
/// many other workers are doing the same thing concurrently — the single
/// largest hidden cost in the whole mapper, and the direct cause of
/// multithreaded whole-genome runs sometimes getting *slower* with more
/// threads (workers that finish this allocation last can end up with zero
/// reads by the time they're ready).
///
/// An intermediate version keyed this by an `FxHashMap<BucketLoc,
/// BucketContent>` instead (same idea: only touched buckets exist, so memory
/// scales with reads, not reference size). That fixed the memory blowup but
/// introduced a *speed* regression on repetitive references: k=15 seeds on a
/// whole genome touch millions of buckets per read, and every touch was a
/// full hashmap `entry()` (hash + probe + possible resize) — on that
/// workload it made single-threaded mapping ~20% *slower* than the original
/// dense array, despite the huge memory win. The fix here keeps the memory
/// win but removes the hashmap from the hot path: `add_to_pos`/
/// `add_to_bucket` just push `(BucketLoc, BucketContent)` onto a flat
/// append-only `Vec` (no hash, no probe, sequential writes), and duplicate
/// locations touched multiple times are merged in one batched sort+dedup
/// pass (`merge_entries`) at the end of the read, right before the results
/// are needed — turning O(hits) random-access hashmap operations into
/// O(hits) cache-friendly appends plus one O(touched log touched) sort.
pub struct Buckets<'idx, const AP: bool> {
    tidx: &'idx SketchIndex,
    pub halflen: QPos,
    pub i: i32,
    pub seeds: i32,
    /// Append-only per-read scratch: every `add_to_pos`/`add_to_bucket` call
    /// pushes one entry, and the same `BucketLoc` may appear many times
    /// before `merge_entries` folds duplicates together.
    entries: Vec<(BucketLoc, BucketContent)>,
    /// Ping-pong buffer for [`Self::radix_sort_entries`], reused (grown,
    /// never shrunk) across reads to avoid reallocating every call.
    radix_scratch: Vec<(BucketLoc, BucketContent)>,
    /// Reused counting-sort histogram for the radix sort, `RADIX_SIZE`
    /// buckets — allocated once, zeroed at the start of each pass.
    radix_counts: Vec<u32>,
    /// Set once `entries` has been sorted+deduplicated, cleared by
    /// `add_to_pos`/`add_to_bucket`/`clear` — lets `merge_entries` (called
    /// once from `propagate_seeds_to_buckets` and again from
    /// `get_sorted_buckets` on every read) skip the second, redundant sort.
    merged: bool,
}

impl<'idx, const AP: bool> Buckets<'idx, AP> {
    pub fn new(tidx: &'idx SketchIndex) -> Self {
        Buckets {
            tidx,
            halflen: -1,
            i: 0,
            seeds: 0,
            entries: Vec::new(),
            radix_scratch: Vec::new(),
            radix_counts: vec![0u32; RADIX_SIZE],
            merged: true,
        }
    }

    /// Clears the per-read scratch buffer for reuse, keeping its
    /// already-allocated capacity (no reallocation across reads).
    pub fn clear(&mut self) {
        self.i = 0;
        self.seeds = 0;
        self.entries.clear();
        self.merged = true;
    }

    /// Sets the bucket half-length; returns `false` if it's below
    /// `MIN_HALFLEN` (the caller should treat that as "too small to map
    /// usefully" rather than a hard error, matching the C++).
    pub fn set_halflen(&mut self, new_halflen: QPos) -> bool {
        self.halflen = new_halflen;
        self.halflen >= MIN_HALFLEN
    }

    pub fn begin(&self, b: &BucketLoc) -> RPos {
        b.b * self.halflen
    }

    pub fn end(&self, b: &BucketLoc) -> RPos {
        (b.b + 2) * self.halflen
    }

    /// Sorts `self.entries` ascending by `(segm_id, b)` in O(n) via an LSD
    /// radix sort on [`radix_key`], instead of an O(n log n) comparison
    /// sort. On repetitive references (k=15 whole-genome, where a read's
    /// seeds can touch millions of raw entries — see the module doc
    /// comment), profiling found this location-sort was ~77% of total
    /// mapping time: a generic comparison sort pays a `log n` factor *and*
    /// a closure call per comparison for what's really just two bounded
    /// integers. Radix sort trades that for a handful of linear passes over
    /// contiguous memory (each pass: one counting scan, one prefix sum over
    /// the small `RADIX_SIZE` histogram, one scatter into `radix_scratch`).
    ///
    /// The number of passes is computed from the actual data rather than
    /// fixed at [`RADIX_MAX_PASSES`]: `segm_id` is a handful of segments and
    /// `b` is bounded by `segment_len / halflen` (a read-scaled bucket
    /// half-length), so the packed key's highest set bit is usually well
    /// below 64 — skipping an always-zero high pass saves a full O(n)
    /// counting+scatter pass for one cheap sequential max-reduce.
    fn radix_sort_entries(&mut self) {
        let n = self.entries.len();
        if n <= 1 {
            return;
        }
        let max_key = self.entries.iter().map(|(loc, _)| radix_key(loc)).max().unwrap_or(0);
        let passes = if max_key == 0 {
            0
        } else {
            (64 - max_key.leading_zeros()).div_ceil(RADIX_BITS).min(RADIX_MAX_PASSES)
        };
        self.radix_scratch.resize(n, (BucketLoc::new(0, 0), BucketContent::default()));
        for pass in 0..passes {
            let shift = pass * RADIX_BITS;
            self.radix_counts.iter_mut().for_each(|c| *c = 0);
            for (loc, _) in &self.entries {
                let key = ((radix_key(loc) >> shift) & RADIX_MASK) as usize;
                self.radix_counts[key] += 1;
            }
            let mut sum = 0u32;
            for c in &mut self.radix_counts {
                let cur = *c;
                *c = sum;
                sum += cur;
            }
            for e in &self.entries {
                let key = ((radix_key(&e.0) >> shift) & RADIX_MASK) as usize;
                self.radix_scratch[self.radix_counts[key] as usize] = *e;
                self.radix_counts[key] += 1;
            }
            std::mem::swap(&mut self.entries, &mut self.radix_scratch);
        }
    }

    /// Sorts `entries` by location and folds every run of matching
    /// `BucketLoc`s into a single entry (summing their `BucketContent` via
    /// `AddAssign`, same semantics as the old per-hit hashmap merge). Safe
    /// to call more than once per read: guarded by `merged` so a repeat call
    /// (see `propagate_seeds_to_buckets`/`get_sorted_buckets`) is a no-op
    /// rather than re-sorting already-deduplicated data.
    fn merge_entries(&mut self) {
        if self.merged {
            return;
        }
        self.merged = true;
        if self.entries.is_empty() {
            return;
        }
        self.radix_sort_entries();
        let mut write = 0usize;
        for read in 1..self.entries.len() {
            if self.entries[read].0 == self.entries[write].0 {
                let c = self.entries[read].1;
                self.entries[write].1 += c;
            } else {
                write += 1;
                self.entries[write] = self.entries[read];
            }
        }
        self.entries.truncate(write + 1);
    }

    pub fn propagate_seeds_to_buckets(&mut self) {
        self.merge_entries();
        let i = self.i;
        let seeds = self.seeds;
        for (_, bc) in &mut self.entries {
            bc.i = i;
            bc.seeds = seeds;
        }
    }

    pub fn add_to_pos(&mut self, hit: &Hit, content: BucketContent) {
        let b = (if AP { hit.r } else { hit.tpos }) / self.halflen;
        debug_assert!((hit.segm_id as usize) < self.tidx.segments_len());
        self.entries.push((BucketLoc::new(hit.segm_id, b), content));
        if b > 0 {
            self.entries.push((BucketLoc::new(hit.segm_id, b - 1), content));
        }
        self.merged = false;
    }

    pub fn add_to_bucket(&mut self, b: BucketLoc, content: BucketContent) {
        self.entries.push((b, content));
        self.merged = false;
    }

    /// Deduplicates the touched buckets and returns them sorted by
    /// descending match count.
    ///
    /// Uses a **stable** sort, unlike the C++'s `std::sort` — ties (equal
    /// `.matches`) get a deterministic relative order here, which the C++
    /// itself doesn't guarantee even between its own runs/compiler
    /// versions. Bit-exact PAF parity against the reference binary isn't a
    /// meaningful target specifically for tied buckets as a result; that's
    /// a property of the reference implementation, not a port regression.
    pub fn get_sorted_buckets(&mut self) -> Vec<(BucketLoc, BucketContent)> {
        self.merge_entries();
        let mut sorted_buckets = self.entries.clone();
        sorted_buckets.sort_by(|a, b| b.1.matches.cmp(&a.1.matches));
        sorted_buckets
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sketch::RefSegment;
    use crate::types::{Kmer, SegmId};

    fn tidx_with_one_segment(sz: RPos) -> SketchIndex {
        let mut tidx = SketchIndex::new();
        tidx.segments.push(RefSegment::new(Vec::new(), "seg0".to_string(), sz, 0));
        tidx
    }

    fn hit(r: RPos, tpos: RPos, segm_id: SegmId) -> Hit {
        Hit::new(&Kmer::new(r, 0, false), tpos, segm_id)
    }

    #[test]
    fn begin_end_bucket_boundaries() {
        let tidx = tidx_with_one_segment(100);
        let mut b: Buckets<false> = Buckets::new(&tidx);
        b.set_halflen(10);

        let b0 = BucketLoc::new(0, 0);
        assert_eq!(b.begin(&b0), 0);
        assert_eq!(b.end(&b0), 20);

        let b1 = BucketLoc::new(0, 1);
        assert_eq!(b.begin(&b1), 10);
        assert_eq!(b.end(&b1), 30);
    }

    #[test]
    fn add_to_pos_touches_bucket_and_predecessor() {
        let tidx = tidx_with_one_segment(100);
        let mut b: Buckets<false> = Buckets::new(&tidx);
        b.set_halflen(10);

        // tpos=25 => bucket 2 (25/10=2), plus predecessor bucket 1.
        b.add_to_pos(&hit(25, 25, 0), BucketContent::new(1, 0, 1, 25, 25));

        let sorted = b.get_sorted_buckets();
        let locs: Vec<BucketLoc> = sorted.iter().map(|(loc, _)| *loc).collect();
        assert_eq!(locs.len(), 2);
        assert!(locs.contains(&BucketLoc::new(0, 1)));
        assert!(locs.contains(&BucketLoc::new(0, 2)));
        for (_, content) in &sorted {
            assert_eq!(content.matches, 1);
        }
    }

    #[test]
    fn add_to_pos_at_bucket_zero_does_not_touch_predecessor() {
        let tidx = tidx_with_one_segment(100);
        let mut b: Buckets<false> = Buckets::new(&tidx);
        b.set_halflen(10);

        b.add_to_pos(&hit(5, 5, 0), BucketContent::new(1, 0, 1, 5, 5));

        let sorted = b.get_sorted_buckets();
        assert_eq!(sorted.len(), 1);
        assert_eq!(sorted[0].0, BucketLoc::new(0, 0));
    }

    #[test]
    fn get_sorted_buckets_dedups_and_orders_by_matches_descending() {
        let tidx = tidx_with_one_segment(200);
        let mut b: Buckets<false> = Buckets::new(&tidx);
        b.set_halflen(10);

        // Two hits landing in the same bucket 5 (and predecessor 4).
        b.add_to_pos(&hit(50, 50, 0), BucketContent::new(1, 0, 1, 50, 50));
        b.add_to_pos(&hit(51, 51, 0), BucketContent::new(1, 0, 1, 51, 51));
        // One hit in a far bucket with fewer matches.
        b.add_to_pos(&hit(150, 150, 0), BucketContent::new(1, 0, 1, 150, 150));

        let sorted = b.get_sorted_buckets();
        // bucket 5 got 2 contributions (matches=2), bucket 4 also 2 (from
        // both add_to_pos calls' predecessor writes), bucket 15/14 got 1.
        assert!(sorted[0].1.matches >= sorted.last().unwrap().1.matches);
        // No duplicate BucketLoc entries.
        let mut locs: Vec<BucketLoc> = sorted.iter().map(|(loc, _)| *loc).collect();
        let before = locs.len();
        locs.sort_by(|a, b| a.segm_id.cmp(&b.segm_id).then(a.b.cmp(&b.b)));
        locs.dedup();
        assert_eq!(locs.len(), before);
    }

    #[test]
    fn clear_resets_touched_buckets() {
        let tidx = tidx_with_one_segment(100);
        let mut b: Buckets<false> = Buckets::new(&tidx);
        b.set_halflen(10);
        b.add_to_pos(&hit(25, 25, 0), BucketContent::new(1, 0, 1, 25, 25));
        assert!(!b.get_sorted_buckets().is_empty());

        b.clear();
        assert!(b.entries.is_empty());
        assert!(b.get_sorted_buckets().is_empty());
    }

    #[test]
    fn abs_pos_flag_selects_r_vs_tpos_for_bucket_index() {
        let tidx = tidx_with_one_segment(1000);
        let mut b_tpos: Buckets<false> = Buckets::new(&tidx);
        b_tpos.set_halflen(10);
        let mut b_abs: Buckets<true> = Buckets::new(&tidx);
        b_abs.set_halflen(10);

        // r=99 (would land in bucket 9), tpos=3 (would land in bucket 0).
        let h = hit(99, 3, 0);
        b_tpos.add_to_pos(&h, BucketContent::new(1, 0, 1, 99, 99));
        b_abs.add_to_pos(&h, BucketContent::new(1, 0, 1, 99, 99));

        let tpos_locs: Vec<BucketLoc> = b_tpos.get_sorted_buckets().into_iter().map(|(l, _)| l).collect();
        let abs_locs: Vec<BucketLoc> = b_abs.get_sorted_buckets().into_iter().map(|(l, _)| l).collect();
        assert!(tpos_locs.contains(&BucketLoc::new(0, 0)));
        assert!(abs_locs.contains(&BucketLoc::new(0, 9)));
    }

    #[test]
    fn buckets_hash_add_to_pos_touches_bucket_and_predecessor() {
        let mut bh: BucketsHash<false> = BucketsHash::new(10);
        bh.add_to_pos(&hit(25, 25, 0), BucketContent::new(1, 0, 1, 25, 25));
        assert_eq!(bh.buckets.len(), 2);
        assert!(bh.buckets.contains_key(&BucketLoc::new(0, 1)));
        assert!(bh.buckets.contains_key(&BucketLoc::new(0, 2)));
    }
}
