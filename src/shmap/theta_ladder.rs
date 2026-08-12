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
//! # Where the rung goes
//!
//! There is exactly one rung below `-t`, and it is placed per read rather than
//! at a fixed threshold, because the useful threshold is a property of the
//! read. A read cannot score above the fraction of its own k-mer occurrences
//! that have *any* hit in the reference — `unique_elements_with_info` has
//! already established that by the time the ladder is built (`hits_in_t == 0`
//! for the rest) — and it lands a measurable distance below that ceiling. The
//! rung goes at `score_ub - RUNG_GAP`, and is taken only if that still saves
//! enough to be worth failing (`MIN_SAVING`).
//!
//! Anchoring on the read is what makes one rung serve two workloads that want
//! opposite things. On HiFi the ceiling is high and the gap to it small, so the
//! rung lands high, costs ~0.2 of the seed budget instead of the ~0.34 a fixed
//! half-budget rung costs, and is cleared nine times in ten. On ONT at
//! `k = 25` the ceiling is low — ~28% of a read's k-mers survive the error
//! rate, under `-t 0.4` — and the gap to it is five times wider, so there is no
//! rung above `-t` worth taking and the read does byte-identical work to the
//! single pass. Both constants carry the measurements behind them.
//!
//! An earlier design placed the rung at half the user's budget regardless and
//! used the ceiling only to decide *whether* to have one. That gave ONT reads
//! with a middling ceiling a rung sitting exactly at it — clearable only by a
//! read achieving its own bound exactly — which `RESULTS.md` §5c measured as a
//! wasted rung on 6% of B05 and the cause of its one regression.

use crate::types::QPos;

/// How far below a read's own score ceiling to place its rung.
///
/// A read cannot score above `score_ub`, the fraction of its k-mers with any
/// hit in the reference — but it does not reach that ceiling either, and the
/// distance is what decides where a rung belongs. Measured over the four
/// benchmark datasets it is small and stable on HiFi and five times wider on
/// ONT:
///
/// | dataset | median gap | share clearing a rung this far below the ceiling |
/// |---|---|---|
/// | B01 real HiFi 23 kb | 0.04 | 93% |
/// | B02 simulated 24 kb | 0.05 | 100% |
/// | B03 real HiFi 13 kb | 0.03 | 98% |
/// | B05 real ONT 23.8 kb | **0.18** | (never offered one — see `MIN_SAVING`) |
///
/// 0.20 is not a guess between them. It is what the cost model of `RESULTS.md`
/// §5d picks, fed each candidate's own counters: over the twelve (benchmark,
/// metric) pairs, 0.10 costs +12.1%, 0.15 +5.6%, 0.25 +9.5% and 0.30 +16.7%
/// against it, and nine of the nine HiFi/simulated pairs choose it
/// individually. Too small a gap places the rung so high that seeding barely
/// covers anything and `seed_heuristic_pass` has to walk the difference — the
/// same trade §5c measures in the other direction. Too large and the rung stops
/// saving.
///
/// B05 is unmoved at *every* value in that sweep, to two decimals: no ONT read
/// is offered a rung at any of them, so the fix this constant carries does not
/// depend on tuning it.
///
/// `SHMAP_THETA_GAP` overrides it.
const RUNG_GAP: f64 = 0.20;

fn rung_gap() -> f64 {
    static GAP: std::sync::OnceLock<f64> = std::sync::OnceLock::new();
    *GAP.get_or_init(|| {
        std::env::var("SHMAP_THETA_GAP")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(RUNG_GAP)
    })
}

/// How much cheaper than `-t` a rung must be before it is worth taking at all.
///
/// A rung that fails costs a wasted merge and sweep, so one that saves almost
/// nothing is a pure loss. Two, i.e. at most half the seed budget — the same
/// line the previous design *placed* the rung at, kept here as a floor on how
/// little it may save. It is what turns a low ceiling into no rung rather than
/// into a rung nobody clears. It is what excludes ONT: a mapped ONT read has a
/// ceiling around 0.68, so `score_ub - RUNG_GAP` lands at 0.48 against a `-t`
/// of 0.4 — a rung saving so little that failing it costs more than skipping
/// it ever saves.
const MIN_SAVING: f64 = 2.0;

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
    theta_final: f64,
    s_final: QPos,
    min_diff: f64,
    m: QPos,
    /// The one rung below `-t`, as a seed budget, if this read has one worth
    /// taking. Consumed when handed out; `None` thereafter and after
    /// [`Self::escalate_to_last`].
    rung: Option<f64>,
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

        // Put the rung just under what this read could possibly score, rather
        // than at a fixed fraction of the user's budget. See `RUNG_GAP`.
        let theta_rung = score_ub - rung_gap();
        let u_rung = 1.0 - theta_rung + min_diff;
        let worth_it = adaptive
            && m > 0
            // Above the user's own threshold, or it is not a rung at all.
            && theta_rung > theta
            // And cheap enough that failing it can be afforded. This is the
            // same half-budget line the fixed-rung design used to *place* the
            // rung at; here it is a floor on how little the rung may save.
            && u_rung * MIN_SAVING <= u_final;

        ThetaLadder {
            theta_final: theta,
            s_final,
            min_diff,
            m,
            rung: worth_it.then_some(u_rung),
            exhausted: false,
        }
    }

    /// Skips the rungs in between and makes the next one the user's own `-t`.
    ///
    /// For the case a rung can map a read but not settle it: an exact-score tie
    /// between two buckets is decided by sweep order, and only `-t`'s own order
    /// is the one the answer is defined by. See [`super::scoring::Sweep::tied`].
    pub fn escalate_to_last(&mut self) {
        self.rung = None;
    }

    /// The next rung, or `None` once the user's own `-t` has been handed out.
    pub fn next_level(&mut self) -> Option<Level> {
        if self.exhausted {
            return None;
        }
        let Some(u) = self.rung.take() else {
            self.exhausted = true;
            return Some(Level {
                theta: self.theta_final,
                s: self.s_final,
                last: true,
            });
        };
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

    fn levels(m: QPos, theta: f64, min_diff: f64, score_ub: f64, adaptive: bool) -> Vec<Level> {
        let mut ladder = ThetaLadder::new(m, theta, min_diff, score_ub, adaptive);
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
    fn a_read_gets_at_most_one_rung_below_its_threshold() {
        for &ub in &[0.0, 0.3, 0.5, 0.7, 0.87, 0.95, 1.0] {
            let ls = levels(M, 0.4, 0.075, ub, true);
            assert!(ls.len() <= 2, "ub={ub}: {ls:?}");
            assert!(ls.iter().all(|l| l.theta >= 0.4), "{ls:?}");
            for w in ls.windows(2) {
                assert!(w[1].theta < w[0].theta, "{ls:?}");
                assert!(w[1].s > w[0].s, "{ls:?}");
            }
        }
    }

    /// The rung is placed against the *read*, not against `-t`: a gap below
    /// whatever that read could possibly score. This is the whole design.
    #[test]
    fn the_rung_sits_one_gap_below_the_ceiling() {
        // `MIN_SAVING` admits a rung only above a ceiling of
        // `1 + min_diff - RUNG_GAP - u_final/2` = 0.9375 at these parameters.
        for &ub in &[0.95, 0.98, 1.0] {
            let ls = levels(M, 0.4, 0.075, ub, true);
            assert_eq!(ls.len(), 2, "ub={ub} should get a rung: {ls:?}");
            assert!(
                (ls[0].theta - (ub - RUNG_GAP)).abs() < 1e-12,
                "ub={ub} rung at {} not at {}",
                ls[0].theta,
                ub - RUNG_GAP
            );
        }
    }

    /// And the case that made the placement worth changing. A read whose
    /// ceiling is only middling gets *no* rung, rather than one sitting so
    /// close to its ceiling that clearing it would need a perfect read — which
    /// is what B05's wasted rung was.
    #[test]
    fn a_middling_ceiling_gets_no_rung_rather_than_an_unclearable_one() {
        // 0.68 is where a mapped ONT read sits; the rest bracket it.
        for &ub in &[0.5, 0.68, 0.8, 0.87, 0.9] {
            let ls = levels(M, 0.4, 0.075, ub, true);
            assert_eq!(ls.len(), 1, "ub={ub} should get no rung: {ls:?}");
            assert!(ls[0].last);
        }
    }

    /// A rung that saves almost nothing is a pure loss when it fails, so it is
    /// not taken at all.
    #[test]
    fn any_rung_taken_saves_at_least_min_saving() {
        for &ub in &[0.5, 0.7, 0.87, 0.95, 1.0] {
            let ls = levels(M, 0.4, 0.075, ub, true);
            if ls.len() < 2 {
                continue;
            }
            let s_final = ls[1].s as f64;
            assert!(
                ls[0].s as f64 * MIN_SAVING <= s_final + MIN_SAVING,
                "ub={ub}: rung S={} against final S={s_final}",
                ls[0].s
            );
        }
    }

    #[test]
    fn a_zero_length_sketch_still_yields_its_one_level() {
        assert_eq!(levels(0, 0.4, 0.075, 0.0, true).len(), 1);
    }
}
