//! `bestFixedLength`, `findBestMapping`, the `lcs*` helpers, `mappingScore`,
//! and `match_rest`.
//!
//! `find_theta_Containment` is dropped: it's confirmed dead (its one call
//! site is commented out in the C++, and it has no test coverage either),
//! matching this port's other confirmed-dead-code removals.

use super::SHMapper;
use crate::buckets::Buckets;
use crate::mapping::Mapping;
use crate::sketch::RefSegment;
use crate::types::{BucketContent, BucketLoc, H2Seed, Kmer, Metric, QPos, RPos, Seeds, codirection_kmer_kmer};

/// Per-read memo of [`SHMapper::find_best_mapping`] results across the two
/// [`SHMapper::match_rest`] passes a read makes (best, then second-best).
///
/// Both passes sweep the *same* `sorted_buckets` slice in the same order,
/// and on the `Containment`/`Jaccard` path `find_best_mapping` reduces to
/// `best_fixed_length(segm, buckets.begin(b), buckets.end(b), p_ht,
/// diff_hist, p_sz - k, lmax, metric)` — every argument of which is either a
/// per-read constant or a pure function of `b`. In particular it does not
/// read `content` (which the pruning pass mutates between the two calls),
/// and it leaves `diff_hist` exactly as it found it: the `l` loop undoes
/// every decrement the `r` loop made, which is what that function's closing
/// `debug_assert_eq!(intersection, 0)` pins. So the second pass recomputes
/// bit-identical results for every bucket the first pass already scored, and
/// memoizing them is output-preserving rather than an approximation. Only
/// `sh` differs between the passes, and that is stamped on after the fact.
///
/// The two passes' refined sets overlap but neither contains the other: pass
/// 1's `thr` ratchets upward as `best` improves, so a bucket pruned late in
/// pass 1 can still clear pass 2's flat `best * (1 - min_diff)`. Misses are
/// therefore normal and are simply recomputed.
///
/// Entries are pushed in `sorted_buckets` order, so replay is a monotone
/// cursor rather than a lookup structure — no hashing, and the whole thing
/// is one reusable `Vec`.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
enum Mode {
    /// Neither recording nor replaying — every bucket is scored from
    /// scratch, exactly as before the memo existed. This is what
    /// `SHMAP_NO_REFINE_MEMO` selects, so one binary can A/B the memo with
    /// nothing else differing.
    #[default]
    Off,
    Record,
    Replay,
}

#[derive(Default)]
pub struct RefineCache {
    entries: Vec<(u32, Mapping)>,
    cursor: usize,
    mode: Mode,
}

impl RefineCache {
    pub fn new() -> Self {
        Self::default()
    }

    /// Whether the memo is compiled-in *and* enabled for this run. Read
    /// once per process, not per read.
    pub fn enabled() -> bool {
        static ENABLED: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
        *ENABLED.get_or_init(|| std::env::var_os("SHMAP_NO_REFINE_MEMO").is_none())
    }

    /// Begins the first pass: forget the previous read and record results.
    pub fn start_recording(&mut self) {
        self.entries.clear();
        self.cursor = 0;
        self.mode = Mode::Record;
    }

    /// Begins the second pass: replay what the first pass recorded, and
    /// stop recording (there is no third pass to serve).
    pub fn start_replay(&mut self) {
        self.cursor = 0;
        self.mode = Mode::Replay;
    }

    /// Index of the memoized result for `sorted_buckets[idx]`, if the
    /// recording pass scored that bucket.
    fn lookup(&mut self, idx: u32) -> Option<usize> {
        if self.mode != Mode::Replay {
            return None;
        }
        // `idx` increases across the sweep and `entries` is in the same
        // order, so this advance is amortized O(1).
        while self.cursor < self.entries.len() && self.entries[self.cursor].0 < idx {
            self.cursor += 1;
        }
        match self.entries.get(self.cursor) {
            Some(&(at, _)) if at == idx => Some(self.cursor),
            _ => None,
        }
    }

    fn get(&self, i: usize) -> &Mapping {
        &self.entries[i].1
    }

    fn record(&mut self, idx: u32, mapping: &Mapping) {
        if self.mode == Mode::Record {
            self.entries.push((idx, mapping.clone()));
        }
    }
}

impl<'idx, const NBP: bool, const OS: bool, const AP: bool> SHMapper<'idx, NBP, OS, AP> {
    /// All query positions (in `p`) whose k-mer hash also appears in
    /// bucket `b`'s span of the reference sketch `t`.
    pub fn lcs_get_ppos_in_t(
        &self,
        t: &[Kmer],
        buckets: &Buckets<'idx, AP>,
        b: &BucketLoc,
        p_ht: &H2Seed,
    ) -> Vec<QPos> {
        let mut ppos_in_t = Vec::with_capacity(p_ht.len());
        let begin = buckets.begin(b);
        let end = (t.len() as RPos).min(buckets.end(b));
        debug_assert!(begin < t.len() as RPos);
        let mut l = begin;
        while l < end {
            if let Some(seed) = p_ht.get(&t[l as usize].h) {
                for &ppos in seed.pmatches.iter() {
                    ppos_in_t.push(ppos);
                }
            }
            l += 1;
        }
        ppos_in_t
    }

    /// Longest increasing subsequence, via patience sorting (`lcs[i]` =
    /// smallest tail value of any increasing subsequence of length `i+1`
    /// seen so far); returns only its *length* via the caller.
    pub fn lcs_get_lis(&self, ppos_in_t: &[QPos]) -> Vec<QPos> {
        let mut lis: Vec<QPos> = Vec::with_capacity(ppos_in_t.len());
        for &x in ppos_in_t {
            let mut l: i64 = -1;
            let mut r: i64 = lis.len() as i64;
            while l + 1 < r {
                let mid = (l + r) / 2;
                if lis[mid as usize] < x {
                    l = mid;
                } else {
                    r = mid;
                }
            }
            if r == lis.len() as i64 {
                lis.push(x);
            } else {
                lis[r as usize] = x;
            }
        }
        lis
    }

    /// Longest common subsequence of query positions matched in bucket
    /// `b`, checked both forward and reverse (to catch either strand).
    pub fn lcs(&self, t: &[Kmer], buckets: &Buckets<'idx, AP>, b: &BucketLoc, p_ht: &H2Seed) -> QPos {
        let mut ppos_in_t = self.lcs_get_ppos_in_t(t, buckets, b, p_ht);
        let lcs_fwd = self.lcs_get_lis(&ppos_in_t);
        ppos_in_t.reverse();
        let lcs_rev = self.lcs_get_lis(&ppos_in_t);
        lcs_fwd.len().max(lcs_rev.len()) as QPos
    }

    pub fn mapping_score(&self, intersection: QPos, m: QPos, s_sz: QPos, metric: Metric) -> f64 {
        match metric {
            Metric::Jaccard => intersection as f64 / (m + s_sz - intersection) as f64,
            Metric::Containment => intersection as f64 / m as f64,
            _ => panic!("Invalid metric for bucket mapping"),
        }
    }

    /// Sweeps `[from, to)` of `segm`'s k-mer sketch (clamped to its
    /// bounds), maintaining `diff_hist` incrementally, tracking the best
    /// `Containment`/`Jaccard` score over windows bounded by `m` (in
    /// reference-position units when `AP`, sketch-index units otherwise).
    ///
    /// This is `SHMapper`'s own `bestFixedLength` — distinct from
    /// `refine::Matcher`'s same-named method (different signature/metric
    /// set; see that module's doc comment for why they're kept separate).
    #[allow(clippy::too_many_arguments)]
    pub fn best_fixed_length(
        &self,
        segm: &RefSegment,
        from: RPos,
        to: RPos,
        p_ht: &H2Seed,
        diff_hist: &mut [QPos],
        p_sz: QPos,
        m: QPos,
        metric: Metric,
    ) -> Mapping {
        let t = &segm.kmers;
        let mut l = from.max(0);
        let mut r = l;
        let end = (t.len() as RPos).min(to);
        debug_assert!(l < t.len() as RPos);

        let mut intersection: QPos = 0;
        let mut same_strand_seeds: i32 = 0;
        let mut best = Mapping::default();

        // Weighted containment runs alongside the plain count rather than
        // replacing it: `intersection` is still what gets reported and what
        // `diff_hist`'s invariant is checked against, so the default path is
        // untouched and the debug_assert at the end still means what it did.
        let weighted = !self.rarity.is_empty();
        let mut w_intersection: f64 = 0.0;

        while l < end {
            let tl = t[l as usize];
            while r < end {
                let tr = t[r as usize];
                let in_window = if AP { tr.r < tl.r + m } else { r < l + m };
                if !in_window {
                    break;
                }
                if let Some(seed) = p_ht.get(&tr.h) {
                    same_strand_seeds += codirection_kmer_kmer(&seed.kmer, &tr);
                    // Indexed by the seed's dense `seed_num`, not hashed again:
                    // this loop sweeps every reference k-mer of every candidate
                    // bucket, and hashing the same key twice (once for `p_ht`,
                    // once for the counter) was the hottest operation in
                    // `refine`.
                    let cnt = &mut diff_hist[seed.seed_num as usize];
                    *cnt -= 1;
                    if *cnt >= 0 {
                        intersection += 1;
                        if weighted {
                            w_intersection += self.rarity[seed.seed_num as usize];
                        }
                    }
                }
                debug_assert!(l <= r);
                r += 1;
            }

            let s_kmers = r - l;
            let score = if weighted {
                (w_intersection / self.m_weight).clamp(0.0, 1.0)
            } else {
                self.mapping_score(intersection, m, s_kmers, metric)
            };
            debug_assert!((-0.0..=1.0).contains(&score));
            if l < r && score > best.score() {
                best.update(
                    0,
                    p_sz - 1,
                    t[l as usize].r,
                    t[(r - 1) as usize].r,
                    segm,
                    intersection,
                    score,
                    same_strand_seeds,
                    t[(r - 1) as usize].r - t[l as usize].r + 1,
                );
            }

            if let Some(seed) = p_ht.get(&tl.h) {
                same_strand_seeds -= codirection_kmer_kmer(&seed.kmer, &tl);
                let cnt = &mut diff_hist[seed.seed_num as usize];
                *cnt += 1;
                if *cnt >= 1 {
                    intersection -= 1;
                    if weighted {
                        w_intersection -= self.rarity[seed.seed_num as usize];
                    }
                }
            }

            debug_assert!(intersection >= 0);
            l += 1;
        }
        debug_assert_eq!(intersection, 0);

        best
    }

    /// Dispatches to the right scoring approach for `metric`, then stamps
    /// the result with `b`/`sh` regardless of which one ran.
    #[allow(clippy::too_many_arguments)]
    pub fn find_best_mapping(
        &self,
        buckets: &Buckets<'idx, AP>,
        b: BucketLoc,
        content: &BucketContent,
        p_ht: &H2Seed,
        diff_hist: &mut [QPos],
        p_sz: QPos,
        m: QPos,
        lmax: QPos,
        sh: f64,
        metric: Metric,
        k: QPos,
    ) -> Mapping {
        let mut best_in_bucket = match metric {
            Metric::BucketSh => {
                let mut mapping = Mapping::default();
                mapping.update(
                    0,
                    p_sz - 1,
                    content.r_min,
                    content.r_max,
                    self.tidx.get_segment(b.segm_id),
                    content.matches,
                    sh,
                    content.codirection,
                    content.r_max - content.r_min,
                );
                mapping
            }
            Metric::BucketLcs => {
                let lcs_cnt = self.lcs(&self.tidx.get_segment(b.segm_id).kmers, buckets, &b, p_ht);
                debug_assert!(content.matches >= lcs_cnt);
                let lcs_score = lcs_cnt as f64 / m as f64;
                debug_assert!((0.0..=1.0).contains(&lcs_score));
                let mut mapping = Mapping::default();
                mapping.update(
                    0,
                    p_sz - 1,
                    content.r_min,
                    content.r_max,
                    self.tidx.get_segment(b.segm_id),
                    content.matches,
                    lcs_score,
                    content.codirection,
                    content.r_max - content.r_min,
                );
                mapping
            }
            Metric::Containment | Metric::Jaccard => self.best_fixed_length(
                self.tidx.get_segment(b.segm_id),
                buckets.begin(&b),
                buckets.end(&b),
                p_ht,
                diff_hist,
                p_sz - k,
                lmax,
                metric,
            ),
        };
        best_in_bucket.set_bucket(b);
        best_in_bucket.set_sh(sh);
        best_in_bucket
    }

    /// Sweeps `sorted_buckets` (best-matches-first), pruning via
    /// [`Self::seed_heuristic_pass`] and scoring survivors via
    /// [`Self::find_best_mapping`]; returns the best mapping clearing
    /// `thr` (and, if `forbidden` is given, not overlapping it by more
    /// than `max_overlap`).
    #[allow(clippy::too_many_arguments)]
    pub fn match_rest(
        &mut self,
        p_sz: QPos,
        m: QPos,
        lmax: QPos,
        p_unique: &Seeds,
        buckets: &Buckets<'idx, AP>,
        sorted_buckets: &mut [(BucketLoc, BucketContent)],
        diff_hist: &mut [QPos],
        p_ht: &H2Seed,
        mut thr: f64,
        forbidden: Option<&Mapping>,
        verbose: i32,
        max_overlap: f64,
        metric: Metric,
        k: QPos,
        cache: &mut RefineCache,
    ) -> Option<Mapping> {
        // Both inert upstream: `lost_on_seeding` is a hardcoded 0 (`int
        // lost_on_seeding = (0);`), and `lost_on_pruning` (threaded through
        // as an out-parameter here in the C++) is never actually written
        // to from a real outcome anywhere in `match_rest` — its caller
        // just always reports 1. Ported as the same inert bumps rather
        // than "fixed", since — unlike the per-read Counters reset bug —
        // these are self-consistent (not unboundedly growing) and only
        // ever feed a diagnostic stat, not a PAF tag.
        self.counters.inc("lost_on_seeding", 0);

        let mut best: Option<Mapping> = None;

        // `find_best_mapping` is only a pure function of the bucket location
        // on the fixed-length metrics; `BucketSh`/`BucketLcs` both build
        // their result out of `content`, which the pruning pass mutates
        // between the two sweeps, so they must not be memoized.
        let memoizable = matches!(metric, Metric::Containment | Metric::Jaccard);

        for (idx, (b, content)) in sorted_buckets.iter_mut().enumerate() {
            let b: BucketLoc = *b;
            let mut sh = 1.0;
            if self.seed_heuristic_pass(buckets, p_unique, m, &b, content, &mut sh, thr) {
                self.timers.start("refine");

                let memo = if memoizable { cache.lookup(idx as u32) } else { None };
                let best_in_bucket = match memo {
                    Some(i) => {
                        self.counters.inc1("refine_memo_hits");
                        // Cloning a `Mapping` allocates (it owns its segment
                        // name), and the large majority of scored buckets
                        // lose to `thr` — so check the memoized score first
                        // and only materialize a winner.
                        if cache.get(i).score() > thr {
                            let mut winner = cache.get(i).clone();
                            winner.set_sh(sh);
                            Some(winner)
                        } else {
                            None
                        }
                    }
                    None => {
                        self.counters.inc1("refined_buckets");
                        let scored =
                            self.find_best_mapping(buckets, b, content, p_ht, diff_hist, p_sz, m, lmax, sh, metric, k);
                        if memoizable {
                            cache.record(idx as u32, &scored);
                        }
                        if scored.score() > thr { Some(scored) } else { None }
                    }
                };

                if let Some(best_in_bucket) = best_in_bucket {
                    if verbose >= 2 {
                        eprintln!("Final bucket: {b} sh: {sh:.3} score: {:.3}", best_in_bucket.score());
                    }
                    self.counters.inc1("final_buckets");
                    if forbidden.is_none_or(|f| Mapping::overlap(&best_in_bucket, f) < max_overlap) {
                        thr = best_in_bucket.score();
                        best = Some(best_in_bucket);
                    }
                }
                self.timers.stop("refine");
            }
        }

        best
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::index::SketchIndex;
    use crate::types::{H2Seed, Seed};

    #[test]
    fn lcs_over_a_bucket_matches_expected_length() {
        let t = vec![
            Kmer::new(10, 0x111111, true),
            Kmer::new(20, 0x222222, true),
            Kmer::new(30, 0x111111, true),
            Kmer::new(40, 0x444444, false),
            Kmer::new(50, 0x555555, false),
            Kmer::new(60, 0x111111, false),
            Kmer::new(70, 0x222222, false),
        ];

        let mut tidx = SketchIndex::new();
        tidx.segments
            .push(RefSegment::new(t.clone(), "test".to_string(), 10, 0));

        let mut buckets: Buckets<false> = Buckets::new(&tidx);
        buckets.set_halflen(2);
        let bucket = BucketLoc::new(0, 1);

        let mut p_ht: H2Seed = H2Seed::default();
        p_ht.insert(
            0x111111,
            Seed::new(Kmer::new(1, 0x111111, false), 99, 3, 0, vec![5, 1].into()),
        );
        p_ht.insert(
            0x222222,
            Seed::new(Kmer::new(2, 0x222222, false), 999, 2, 1, vec![4, 2].into()),
        );
        p_ht.insert(
            0x444444,
            Seed::new(Kmer::new(3, 0x444444, false), 9, 1, 2, vec![3].into()),
        );

        let mapper: SHMapper<false, false, false> = SHMapper::new(&tidx);
        let lcs_cnt = mapper.lcs(&t, &buckets, &bucket, &p_ht);
        assert_eq!(lcs_cnt, 3);
    }
}
