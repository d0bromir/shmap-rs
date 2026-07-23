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
/// Every operation here already only ever touches buckets recorded in
/// `non_empty_buckets_with_repeats` — never a scan of the whole store — so
/// switching to a sparse `FxHashMap` (the same backing type
/// [`BucketsHash`] above already uses as per-seed scratch space) needs no
/// algorithmic change, just a storage swap: memory now scales with how many
/// buckets a worker's reads actually touch, not with reference size, and
/// there's no more up-front allocation to pay for at all.
pub struct Buckets<'idx, const AP: bool> {
    tidx: &'idx SketchIndex,
    pub halflen: QPos,
    pub i: i32,
    pub seeds: i32,
    buckets: FxHashMap<BucketLoc, BucketContent>,
    pub non_empty_buckets_with_repeats: Vec<BucketLoc>,
}

impl<'idx, const AP: bool> Buckets<'idx, AP> {
    pub fn new(tidx: &'idx SketchIndex) -> Self {
        Buckets {
            tidx,
            halflen: -1,
            i: 0,
            seeds: 0,
            buckets: FxHashMap::default(),
            non_empty_buckets_with_repeats: Vec::new(),
        }
    }

    /// Removes all buckets touched since the last `clear()` (rather than
    /// resetting them to default in place) so the map's size tracks "buckets
    /// touched by the most recent read(s)", not every bucket ever touched
    /// across this worker's whole lifetime.
    pub fn clear(&mut self) {
        self.i = 0;
        self.seeds = 0;
        for loc in self.non_empty_buckets_with_repeats.drain(..) {
            self.buckets.remove(&loc);
        }
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

    pub fn propagate_seeds_to_buckets(&mut self) {
        for loc in &self.non_empty_buckets_with_repeats {
            let bc = self
                .buckets
                .get_mut(loc)
                .expect("non_empty_buckets_with_repeats loc must have a bucket entry");
            bc.i = self.i;
            bc.seeds = self.seeds;
        }
    }

    pub fn add_to_pos(&mut self, hit: &Hit, content: BucketContent) {
        let b = (if AP { hit.r } else { hit.tpos }) / self.halflen;
        debug_assert!((hit.segm_id as usize) < self.tidx.segments_len());
        let loc = BucketLoc::new(hit.segm_id, b);
        *self.buckets.entry(loc).or_default() += content;
        self.non_empty_buckets_with_repeats.push(loc);
        if b > 0 {
            let prev_loc = BucketLoc::new(hit.segm_id, b - 1);
            *self.buckets.entry(prev_loc).or_default() += content;
            self.non_empty_buckets_with_repeats.push(prev_loc);
        }
    }

    pub fn add_to_bucket(&mut self, b: BucketLoc, content: BucketContent) {
        *self.buckets.entry(b).or_default() += content;
        self.non_empty_buckets_with_repeats.push(b);
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
        self.non_empty_buckets_with_repeats
            .sort_by(|a, b| a.segm_id.cmp(&b.segm_id).then(a.b.cmp(&b.b)));
        self.non_empty_buckets_with_repeats.dedup();

        let mut sorted_buckets: Vec<(BucketLoc, BucketContent)> = self
            .non_empty_buckets_with_repeats
            .iter()
            .map(|loc| (*loc, self.buckets[loc]))
            .collect();
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
        assert!(b.non_empty_buckets_with_repeats.is_empty());
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
