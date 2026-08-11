//! The descending-threshold ladder `map_read` maps each read against.
//!
//! Not in the C++: this is an exact reformulation of the single pass at
//! `-t`, not an approximation of it. The threshold enters mapping in exactly
//! two places — the seed count `S = (1 - (t - d)) * m + 1`, and the initial
//! `thr` of the bucket sweep — and *both* get cheaper as `t` rises. So rather
//! than paying once for the user's `t`, a read is tried at a sequence of
//! thresholds descending towards it, stopping at the first one that maps.
//!
//! # Why stopping early is exact, not a heuristic
//!
//! Let `t` be the user's threshold, `t' >= t` the threshold of the level that
//! produced a mapping, and `B > t'` that mapping's score. Every bucket the
//! full run at `t` would have considered falls into one of three cases, and
//! none of them can beat `B`:
//!
//! 1. **Seeded and scored at `t'` too.** Both runs score a bucket with
//!    [`SHMapper::find_best_mapping`], which on the fixed-length metrics is a
//!    pure function of the bucket's *location* (see [`super::scoring::RefineCache`]),
//!    and on `bucket_SH`/`bucket_LCS` reads a bucket state that is complete —
//!    [`SHMapper::seed_heuristic_pass`] only returns `true` after consuming
//!    *every* seed, so a scored bucket carries the same content either way.
//!    Same bucket, same score.
//!
//! 2. **Seeded at `t'` but pruned there.** Pruning drops a bucket when its
//!    seed-heuristic bound `sh` falls under the sweep's running `thr`, and
//!    `thr` never exceeds `max(t', B) = B` (it starts at `t'` and ratchets to
//!    the best score found). `sh` is an upper bound on the bucket's score, so
//!    `score <= sh < B`.
//!
//! 3. **Not seeded at `t'` at all.** This is the seed heuristic's own
//!    guarantee, and it is why `S` is defined the way it is: a bucket whose
//!    score reaches `t' - d` misses at most `(1 - (t' - d)) * m` of the read's
//!    k-mer occurrences, so it cannot dodge all `S > (1 - (t' - d)) * m` of
//!    them. An unseeded bucket therefore scores below `t' - d < t' < B`.
//!
//! The same three cases bound the *second-best* sweep, whose flat threshold
//! `B * (1 - d)` is at least `B - d > t' - d` because `B <= 1` — so a bucket
//! missed by seeding at `t'` cannot qualify as the runner-up either, and the
//! mapq computed from it is unchanged. Escalating is exact for the same
//! reason in reverse: a level that maps nothing has proved only that no
//! bucket scores above *its* threshold, which says nothing about `t`.
//!
//! Two configurations are excluded from all of this by
//! [`ThetaLadder::new`]'s `adaptive` argument rather than being quietly
//! approximated — see [`super::SHMapper::map_read`] for where that is decided.
//!
//! # Where the ladder starts
//!
//! Not at 1.0. A read cannot score above the fraction of its own k-mer
//! occurrences that have *any* hit in the reference at all, which
//! `unique_elements_with_info` has already established by the time the
//! ladder is built (`hits_in_t == 0` for the rest). Starting one score
//! quantum `1/m` below that bound skips every level a read could not have
//! passed anyway, and it is what keeps the ladder from costing anything on
//! reads that do not map: at `k = 25`, ONT's ~5-10% error leaves only ~28% of
//! a read's k-mers intact, well under `-t 0.4`, so those reads get a
//! one-level ladder — byte-identical work to the single pass they do today.
//!
//! The rungs are then laid out by *halving down from the user's own budget*
//! until one falls at or below that starting point, rather than by doubling up
//! from it. Same geometric spacing, but anchored at the end that has to be
//! exact: the last rung is `-t` itself and not a value that merely rounds to
//! it, and no rung lands a couple of seeds short of it. Halving also bounds
//! the whole ladder's seeding at less than twice its final rung's — and in
//! practice well under that, because seeds are consumed rarest-first, so a
//! budget's expensive half is always its tail.
//!
//! How *many* rungs is [`DEFAULT_STEPS`], and the answer measured there is one,
//! which is worth stating plainly: this makes the ladder a single cheaper
//! attempt before `-t`, not a long descent. Its comment explains why more is
//! worse.

use crate::types::QPos;

/// How much the seed budget grows per level.
///
/// 2.0 is the doubling that bounds the whole ladder's seeding at twice its
/// final level's, the same argument a growing `Vec` makes about reallocation.
/// Lower would place rungs closer to each read's actual score and waste less
/// of the last step; higher would cut the number of `get_sorted_buckets`
/// rebuilds. Measured both ways — see `RESULTS.md`.
const GROWTH: f64 = 2.0;

/// How many rungs may sit below the user's own `-t`. Measured, not assumed.
///
/// This is the ladder's one real tuning knob, and it is not "as many as
/// possible", because the two halves of the seed heuristic move in *opposite*
/// directions as `S` falls: seeding streams fewer k-mer hit lists, but
/// `seed_heuristic_pass` then has to extend every bucket that survives its
/// first check over the seeds seeding no longer covered — and the second cost
/// grows faster than the first shrinks. Sweeping 0 (the single pass), 1, 2 and
/// unbounded across B01/B03/B05 × three metrics put the optimum at 1 for five
/// of the six real (benchmark, metric) pairs, and within noise of the optimum
/// on the sixth. See `RESULTS.md` §5c for the table.
///
/// It also bounds the ladder unconditionally: without a cap, `--min_diff 0`
/// would admit up to `log2(m)` rungs.
const DEFAULT_STEPS: u32 = 1;

/// [`DEFAULT_STEPS`], or the `SHMAP_THETA_STEPS` override — which exists so the
/// sweep above can be re-run against a new dataset without a rebuild. `0` is
/// exactly the single pass. Read once per process, not per read.
fn max_steps() -> u32 {
    static STEPS: std::sync::OnceLock<u32> = std::sync::OnceLock::new();
    *STEPS.get_or_init(|| {
        std::env::var("SHMAP_THETA_STEPS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(DEFAULT_STEPS)
    })
}

/// Whether the ladder is enabled for this run. Read once per process, not per
/// read. `SHMAP_NO_ADAPTIVE_THETA` collapses every read to the single pass at
/// `-t`, so one binary can A/B the ladder with nothing else differing — the
/// same switch `SHMAP_NO_REFINE_MEMO` gives the refine memo.
pub fn enabled() -> bool {
    static ENABLED: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *ENABLED.get_or_init(|| std::env::var_os("SHMAP_NO_ADAPTIVE_THETA").is_none())
}

/// One rung: the threshold to sweep at, and the seed count it implies.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Level {
    /// The `thr` this level's bucket sweep starts from.
    pub theta: f64,
    /// `S`, the number of read k-mer occurrences to seed with.
    pub s: QPos,
    /// Whether this is the user's own `-t`, i.e. the last rung there is.
    pub last: bool,
}

/// The sequence of levels for one read, cheapest first.
///
/// Always yields at least one level, and its last level is always exactly the
/// user's `-t` with exactly the `S` the single-pass code computed — so a read
/// that escalates all the way does bit-identical work to a run without the
/// ladder, and `adaptive: false` collapses to precisely that.
pub struct ThetaLadder {
    /// The user's own seed budget as a fraction of `m`: `1 - (theta -
    /// min_diff)`, the share of the read's k-mer occurrences a bucket is
    /// allowed to miss and still be found by seeding.
    u_final: f64,
    theta_final: f64,
    s_final: QPos,
    min_diff: f64,
    m: QPos,
    /// Rungs still below the final one. Each is `u_final` halved that many
    /// times, so `0` means the next rung *is* the final one.
    left: u32,
    exhausted: bool,
}

impl ThetaLadder {
    /// Builds the ladder for a read of sketch size `m` whose score cannot
    /// exceed `score_ub`.
    ///
    /// `adaptive: false` yields the single level at `theta`, for the callers
    /// whose output the early stop would not preserve.
    pub fn new(m: QPos, theta: f64, min_diff: f64, score_ub: f64, adaptive: bool) -> Self {
        let u_final = 1.0 - (theta - min_diff);
        // Written exactly as the single-pass code wrote it, not merely
        // equivalently: the last level has to reproduce it bit for bit.
        let s_final = ((1.0 - (theta - min_diff)) * m as f64) as QPos + 1;

        // The cheapest rung worth trying: one score quantum below the highest
        // score this read could possibly reach. At `score_ub` itself the
        // sweep's strict `score > thr` could never fire, so that rung would be
        // a guaranteed waste.
        let u_start = if adaptive && m > 0 {
            1.0 - (score_ub - 1.0 / m as f64) + min_diff
        } else {
            u_final
        };

        // Halve `u_final` while the result still stands above `u_start`.
        // `floor` and not `ceil`: it keeps the first rung at or above
        // `u_start`, so the ladder never opens with a rung the read was
        // already known not to clear.
        let left = if u_start < u_final {
            ((u_final / u_start).log2().floor() as u32).min(max_steps())
        } else {
            0
        };

        ThetaLadder {
            u_final,
            theta_final: theta,
            s_final,
            min_diff,
            m,
            left,
            exhausted: false,
        }
    }

    /// Skips the rungs in between and makes the next one the user's own `-t`.
    ///
    /// For the case a rung can map a read but not settle it: an exact-score tie
    /// between two buckets is decided by sweep order, and only `-t`'s own order
    /// is the one the answer is defined by. See [`super::scoring::Sweep::tied`].
    pub fn escalate_to_last(&mut self) {
        self.left = 0;
    }

    /// The next rung, or `None` once the user's own `-t` has been handed out.
    pub fn next_level(&mut self) -> Option<Level> {
        if self.exhausted {
            return None;
        }
        if self.left == 0 {
            self.exhausted = true;
            return Some(Level {
                theta: self.theta_final,
                s: self.s_final,
                last: true,
            });
        }
        // Recomputed from `u_final` rather than carried and multiplied, so
        // that repeated halving cannot drift the rungs off the anchor.
        let u = self.u_final / GROWTH.powi(self.left as i32);
        self.left -= 1;
        Some(Level {
            theta: 1.0 - u + self.min_diff,
            s: (u * self.m as f64) as QPos + 1,
            last: false,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The rung arithmetic has to hold for any cap, so the tests drive it
    /// through the unbounded ladder rather than through `DEFAULT_STEPS` —
    /// which is a tuning result and free to change without these becoming
    /// wrong. `SHMAP_THETA_STEPS` is process-wide and read once, so it cannot
    /// be set from inside a test binary that runs its tests in parallel.
    fn levels(m: QPos, theta: f64, min_diff: f64, score_ub: f64, adaptive: bool) -> Vec<Level> {
        let mut ladder = ThetaLadder::new(m, theta, min_diff, score_ub, adaptive);
        ladder.left = if adaptive && m > 0 && score_ub > theta {
            let u_final = 1.0 - (theta - min_diff);
            let u_start = 1.0 - (score_ub - 1.0 / m as f64) + min_diff;
            if u_start < u_final {
                (u_final / u_start).log2().floor() as u32
            } else {
                0
            }
        } else {
            ladder.left
        };
        let mut out = Vec::new();
        while let Some(l) = ladder.next_level() {
            out.push(l);
        }
        out
    }

    /// The paper parameters, with the sketch size of a 12.8 kb read.
    const M: QPos = 128;

    #[test]
    fn disabled_is_exactly_the_single_pass() {
        let ls = levels(M, 0.4, 0.075, 1.0, false);
        assert_eq!(ls.len(), 1);
        assert_eq!(ls[0].theta, 0.4);
        assert_eq!(ls[0].s, ((1.0 - 0.325) * M as f64) as QPos + 1);
        assert!(ls[0].last);
    }

    #[test]
    fn last_level_always_reproduces_the_single_pass() {
        for &ub in &[0.0, 0.3, 0.5, 0.87, 0.99, 1.0] {
            let ls = levels(M, 0.4, 0.075, ub, true);
            let last = ls.last().unwrap();
            assert!(last.last);
            assert_eq!(last.theta, 0.4);
            assert_eq!(last.s, ((1.0 - 0.325) * M as f64) as QPos + 1);
            // ... and it is the only level that claims to be last.
            assert_eq!(ls.iter().filter(|l| l.last).count(), 1);
        }
    }

    #[test]
    fn a_read_that_cannot_reach_the_threshold_gets_one_level() {
        // ONT at k=25: ~28% of k-mers survive the error rate, under `-t 0.4`,
        // so there is nothing above the user's own threshold to try first.
        assert_eq!(levels(M, 0.4, 0.075, 0.28, true).len(), 1);
    }

    #[test]
    fn thresholds_descend_and_budgets_grow() {
        let ls = levels(M, 0.4, 0.075, 0.99, true);
        assert!(ls.len() > 1, "a near-perfect read should have rungs to climb");
        for w in ls.windows(2) {
            assert!(w[1].theta < w[0].theta, "{:?}", ls);
            assert!(w[1].s > w[0].s, "{:?}", ls);
        }
        assert!(ls.iter().all(|l| l.theta >= 0.4), "{ls:?}");
        assert!(ls[0].s >= 1);
    }

    /// The property the exactness argument rests on: at every level, seeding
    /// covers strictly more than the k-mer occurrences a bucket scoring
    /// `theta - min_diff` is allowed to miss.
    #[test]
    fn every_level_satisfies_the_seed_heuristic_bound() {
        for &ub in &[0.5, 0.75, 0.9, 1.0] {
            for &(theta, min_diff) in &[(0.4, 0.075), (0.9, 0.02), (0.15, 0.075), (0.5, 0.0)] {
                for l in levels(M, theta, min_diff, ub, true) {
                    let allowed_misses = (1.0 - (l.theta - min_diff)) * M as f64;
                    assert!(
                        l.s as f64 > allowed_misses,
                        "S={} <= {allowed_misses} at theta={} (ub={ub})",
                        l.s,
                        l.theta
                    );
                }
            }
        }
    }

    /// Doubling is what bounds the ladder's overhead: everything before any
    /// rung costs less than that rung does.
    #[test]
    fn the_budget_before_any_level_is_under_that_level() {
        for &ub in &[0.5, 0.8, 0.95, 1.0] {
            let ls = levels(M, 0.4, 0.075, ub, true);
            for i in 1..ls.len() {
                let before: QPos = ls[..i].iter().map(|l| l.s).sum();
                // `+ i` slack: each rung's `S` carries the formula's own `+ 1`.
                assert!(before <= ls[i].s + i as QPos, "{ls:?} at {i}");
            }
        }
    }

    /// The first rung must be one the read could actually clear — the whole
    /// reason the ladder is anchored on `score_ub` — and no rung may ever ask
    /// for *more* than the user did.
    #[test]
    fn the_first_rung_is_reachable_and_no_rung_is_stricter_than_needed() {
        for &ub in &[0.5, 0.87, 0.95, 1.0] {
            let ls = levels(M, 0.4, 0.075, ub, true);
            // Reachable: a read scoring its own upper bound clears rung 0.
            assert!(ub > ls[0].theta, "ub={ub} first theta={}", ls[0].theta);
            assert!(ls.iter().all(|l| l.theta >= 0.4), "{ls:?}");
        }
        // Below `-t` there is nothing above the user's own threshold to try
        // first, and the ladder collapses to it rather than opening lower.
        let hopeless = levels(M, 0.4, 0.075, 0.3, true);
        assert_eq!(hopeless.len(), 1);
        assert_eq!(hopeless[0].theta, 0.4);
    }

    /// No rung may be a near-duplicate of the one after it: each costs a full
    /// bucket rebuild and sweep, so one that seeds barely more than its
    /// predecessor is pure overhead.
    #[test]
    fn rungs_are_a_real_factor_apart() {
        for &ub in &[0.5, 0.87, 0.95, 1.0] {
            let ls = levels(M, 0.4, 0.075, ub, true);
            for w in ls.windows(2) {
                assert!(w[1].s >= 2 * w[0].s - 1, "{ls:?}");
            }
        }
    }

    #[test]
    fn a_zero_length_sketch_still_yields_its_one_level() {
        assert_eq!(levels(0, 0.4, 0.075, 0.0, true).len(), 1);
    }
}
