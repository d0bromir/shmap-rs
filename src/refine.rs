//! Match collection and Jaccard/Containment scoring used by ground-truth
//! analysis (`analyse_simulated.rs`).
//!
//! Port of `shmap/src/refine.h`.
//!
//! `Matcher` is a distinct, narrower type from `shmap::SHMapper`'s own
//! (differently-shaped) `bestFixedLength`/`findBestMapping` — the C++ has
//! this same mild duplication (two same-named-in-spirit methods with
//! different signatures/metric enums) since `Matcher` is only ever used
//! from the verbose/ground-truth path, never the main mapping hot path.
//! Kept as two separate types here too rather than force-unified, so
//! `Matcher`'s callers can't be handed a `bucket_SH`/`bucket_LCS` variant
//! they were never designed to accept.
//!
//! `Matcher::do_overlap`/`lost_correct_mapping` have zero call sites
//! anywhere in the C++ (confirmed via grep — only their own definitions,
//! and one commented-out call site) and are dropped, matching how
//! `Buckets::size()`'s dead stub was dropped.

use crate::buckets::Buckets;
use crate::index::SketchIndex;
use crate::mapping::Mapping;
use crate::types::{BucketLoc, H2Cnt, H2Seed, Match, Matches, QPos, RPos};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum MappingMetric {
    Jaccard,
    ContainmentIndex,
}

pub fn containment_index(intersection: QPos, m: QPos) -> f64 {
    debug_assert!(intersection <= m);
    intersection as f64 / m as f64
}

pub fn jaccard(intersection: QPos, m: QPos, s_sz: QPos) -> f64 {
    debug_assert!(intersection <= s_sz);
    debug_assert!(intersection <= m);
    intersection as f64 / (m + s_sz - intersection) as f64
}

pub struct Matcher<'idx, 'b, const AP: bool> {
    tidx: &'idx SketchIndex,
    buckets: &'b Buckets<'idx, AP>,
    diff_hist: H2Cnt,
}

impl<'idx, 'b, const AP: bool> Matcher<'idx, 'b, AP> {
    pub fn new(tidx: &'idx SketchIndex, buckets: &'b Buckets<'idx, AP>, diff_hist: H2Cnt) -> Self {
        Matcher {
            tidx,
            buckets,
            diff_hist,
        }
    }

    pub fn update_diff_hist(&mut self, diff_hist: H2Cnt) {
        self.diff_hist = diff_hist;
    }

    /// All (seed, hit) pairs in bucket `b` whose k-mer hash is present in
    /// `p_ht`.
    ///
    /// The C++ doesn't clamp `end` to `segm.kmers.len()` before indexing
    /// (a bucket's nominal end can run past the segment's sketch when it's
    /// the last bucket) — a latent out-of-bounds read there. Clamped here
    /// to stay safe; this mirrors the clamp already present in the C++'s
    /// own (unused) `do_overlap` for the same `B.end(b)` value.
    pub fn collect_matches<'p>(&self, b: &BucketLoc, p_ht: &'p H2Seed) -> Matches<'p> {
        let segm = self.tidx.get_segment(b.segm_id);
        let (start, end) = if AP {
            let start = segm.kmers.partition_point(|k| k.r < self.buckets.begin(b));
            let end = segm.kmers.partition_point(|k| k.r < self.buckets.end(b));
            (start, end)
        } else {
            (self.buckets.begin(b) as usize, self.buckets.end(b) as usize)
        };
        let end = end.min(segm.kmers.len());

        let mut m = Matches::new();
        for i in start..end {
            let kmer = segm.kmers[i];
            if let Some(seed) = p_ht.get(&kmer.h) {
                m.push(Match::new(seed, crate::types::Hit::new(&kmer, i as RPos, b.segm_id)));
            }
        }
        m
    }

    /// Best Jaccard mapping over windows `[l, r)` of `matches` whose
    /// reference-position span (in sketch-index units) is within
    /// `(lmin, lmax]` of `l`'s position — a two-pointer sweep incrementally
    /// maintaining `diff_hist` as the window shifts.
    ///
    /// `matches` must be sorted ascending by `hit.tpos` (as `collect_matches`
    /// produces it).
    pub fn best_included_jaccard(
        &mut self,
        matches: &[Match],
        p_sz: QPos,
        lmin: RPos,
        lmax: RPos,
        m: QPos,
    ) -> Mapping {
        let n = matches.len();
        let mut intersection: QPos = 0;
        let mut same_strand_seeds: i32 = 0;
        let mut best = Mapping::default();

        let mut l: usize = 0;
        let mut r: usize = 0;
        while l < n {
            // Shrink the window from the right while it's wider than
            // `lmin`. Guarded with `r < n`: the C++ dereferences `r` here
            // even when `r == M.end()` (undefined behavior there) — see
            // the module doc comment on why this port treats "nothing at
            // `r`" as "condition false" instead of reproducing that.
            while l + 1 < r && r < n && matches[l].hit.tpos + lmin < matches[r].hit.tpos {
                r -= 1;
                let h = matches[r].seed.kmer.h;
                let cnt = self.diff_hist.entry(h).or_insert(0);
                *cnt += 1;
                if *cnt >= 1 {
                    intersection -= 1;
                    same_strand_seeds -= matches[r].codirection();
                }
            }

            // Grow the window from the right while within `lmax`.
            while r < n && matches[r].hit.tpos <= matches[l].hit.tpos + lmax {
                let h = matches[r].seed.kmer.h;
                let cnt = self.diff_hist.entry(h).or_insert(0);
                *cnt -= 1;
                if *cnt >= 0 {
                    intersection += 1;
                    same_strand_seeds += matches[r].codirection();
                }
                debug_assert!(matches[l].hit.r <= matches[r].hit.r);
                if l < r {
                    let s_sz = matches[r - 1].hit.tpos - matches[l].hit.tpos + 1;
                    let j = jaccard(intersection, m, s_sz);
                    best.update(
                        0,
                        p_sz - 1,
                        matches[l].hit.r,
                        matches[r - 1].hit.r,
                        self.tidx.get_segment(matches[l].hit.segm_id),
                        intersection,
                        j,
                        same_strand_seeds,
                        s_sz,
                    );
                }
                r += 1;
            }

            let h = matches[l].seed.kmer.h;
            let cnt = self.diff_hist.entry(h).or_insert(0);
            *cnt += 1;
            if *cnt >= 1 {
                intersection -= 1;
                same_strand_seeds -= matches[l].codirection();
            }
            debug_assert!(intersection >= 0);
            l += 1;
        }
        debug_assert_eq!(intersection, 0);

        best
    }

    /// Best Jaccard/Containment mapping over windows `[l, r)` bounded by
    /// `hit.r <= l.hit.r + P_sz` (a *read-length*-wide window, unlike
    /// `SHMapper`'s own `bestFixedLength` which sweeps a fixed `lmax`).
    ///
    /// The C++ signature also takes an `lmax` parameter that every call
    /// site dutifully passes `2*bucket_l`/`bucket_l` into — but the window
    /// bound actually used in the body is `P_sz`, not `lmax` (there's a
    /// commented-out `lmax`-based condition directly above the live one,
    /// suggesting the author switched approaches and left the now-unused
    /// parameter in place). Dropped here rather than kept as a silent
    /// no-op, consistent with this port's other confirmed-dead-code
    /// removals (`Buckets::size()`, the `sam` flag, ...).
    pub fn best_fixed_length(&mut self, matches: &[Match], p_sz: QPos, m: QPos, metric: MappingMetric) -> Mapping {
        let n = matches.len();
        let mut intersection: QPos = 0;
        let mut same_strand_seeds: i32 = 0;
        let mut best = Mapping::default();

        let mut l: usize = 0;
        let mut r: usize = 0;
        while l < n {
            while r < n && matches[r].hit.r <= matches[l].hit.r + p_sz {
                let h = matches[r].seed.kmer.h;
                let cnt = self.diff_hist.entry(h).or_insert(0);
                *cnt -= 1;
                if *cnt >= 0 {
                    intersection += 1;
                    same_strand_seeds += matches[r].codirection();
                }
                debug_assert!(matches[l].hit.r <= matches[r].hit.r);
                r += 1;
            }

            if r > 0 {
                let s_sz = matches[r - 1].hit.tpos - matches[l].hit.tpos + 1;
                let j = match metric {
                    MappingMetric::Jaccard => jaccard(intersection, m, s_sz),
                    MappingMetric::ContainmentIndex => containment_index(intersection, m),
                };
                debug_assert!((-0.0..=1.0).contains(&j));
                if j > best.score() {
                    best.update(
                        0,
                        p_sz - 1,
                        matches[l].hit.r,
                        matches[r - 1].hit.r,
                        self.tidx.get_segment(matches[l].hit.segm_id),
                        intersection,
                        j,
                        same_strand_seeds,
                        s_sz,
                    );
                }
            }

            let h = matches[l].seed.kmer.h;
            let cnt = self.diff_hist.entry(h).or_insert(0);
            *cnt += 1;
            if *cnt >= 1 {
                intersection -= 1;
                same_strand_seeds -= matches[l].codirection();
            }
            debug_assert!(intersection >= 0);
            l += 1;
        }
        debug_assert_eq!(intersection, 0);

        best
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn jaccard_and_containment_formulas() {
        let intersection = 5;
        let m = 10;
        let s_sz = 15;
        assert!((jaccard(intersection, m, s_sz) - 5.0 / 20.0).abs() < 1e-9);
        assert!((containment_index(intersection, m) - 0.5).abs() < 1e-9);
    }
}
