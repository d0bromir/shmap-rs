//! `hseed`, `matches_in_bucket`, `seed_heuristic_pass`.

use super::SHMapper;
use crate::buckets::Buckets;
use crate::types::{BucketContent, BucketLoc, QPos, RPos, Seed, Seeds};

impl<'idx, const NBP: bool, const OS: bool, const AP: bool> SHMapper<'idx, NBP, OS, AP> {
    /// Seed-heuristic score: fraction of seeds (out of `p`, the sketch
    /// size) that found a match, in `[0, 1]` for a well-formed bucket.
    pub fn hseed(&self, p: QPos, seeds: QPos, matches: QPos) -> f64 {
        debug_assert!(seeds >= matches);
        1.0 - (seeds - matches) as f64 / p as f64
    }

    /// Extends `bucket` with the matches of a single additional seed `s`,
    /// mirroring `Buckets::add_to_pos`'s "does this hit fall inside this
    /// bucket's span" check but for an already-known bucket location.
    pub fn matches_in_bucket(&self, buckets: &Buckets<'idx, AP>, b: &BucketLoc, bucket: &mut BucketContent, s: &Seed) {
        bucket.seeds += s.occs_in_p;
        if s.hits_in_t == 0 {
            // nothing to add
        } else if s.hits_in_t == 1 {
            let hit = self.tidx.single_hit(s.kmer.h);
            // Fixed vs. upstream: the C++'s single-hit branch tests only the
            // position, never `segm_id` — while its multi-hit branch checks
            // `segm_id` in both the `lower_bound` and the loop. A k-mer whose
            // one genome-wide hit lies in a *different* segment, at a `tpos`
            // (or `r`) that happens to fall in this bucket's span, was
            // therefore counted into the bucket: its `matches` inflated `sh`
            // and so weakened pruning, and its `r` — a coordinate in the wrong
            // segment — merged into `r_min`/`r_max`. That is invisible under
            // `Containment`/`Jaccard`, which recompute coordinates in
            // `best_fixed_length` and discard `r_min`/`r_max`, but `bucket_SH`
            // reports them directly and emitted positions past the end of the
            // segment (measured: an end 1.28 Mb beyond chr6 on real HG002
            // HiFi). Found by validating output invariants rather than by
            // diffing against a previous build, which cannot catch a defect
            // both builds share.
            let in_range = hit.segm_id == b.segm_id
                && if AP {
                    buckets.begin(b) <= hit.r && hit.r < buckets.end(b)
                } else {
                    buckets.begin(b) <= hit.tpos && hit.tpos < buckets.end(b)
                };
            if in_range {
                bucket.matches += 1;
                bucket.codirection += if hit.strand == s.kmer.strand { 1 } else { -1 };
                bucket.r_min = bucket.r_min.min(hit.r);
                bucket.r_max = bucket.r_max.max(hit.r);
            }
        } else {
            let hits = self.tidx.multi_hits(s.kmer.h);
            let start = hits.partition_point(|hit| {
                if hit.segm_id != b.segm_id {
                    hit.segm_id < b.segm_id
                } else if AP {
                    hit.r < buckets.begin(b)
                } else {
                    hit.tpos < buckets.begin(b)
                }
            });

            let mut matches: RPos = 0;
            for hit in &hits[start..] {
                let in_range = if AP {
                    hit.segm_id == b.segm_id && hit.r < buckets.end(b)
                } else {
                    hit.segm_id == b.segm_id && hit.tpos < buckets.end(b)
                };
                if !in_range {
                    break;
                }
                matches += 1;
                bucket.codirection += if hit.strand == s.kmer.strand { 1 } else { -1 };
                bucket.r_min = bucket.r_min.min(hit.r);
                bucket.r_max = bucket.r_max.max(hit.r);
            }
            bucket.matches += matches.min(s.occs_in_p);
        }
    }

    /// Incrementally extends `bucket` with more seeds while its
    /// seed-heuristic upper bound still clears `thr`; returns `false` the
    /// moment it can't (bucket is prunable) and `true` if it reaches the end of
    /// `p_unique` — or, when `skip` is set, `true` as soon as the first `sh`
    /// test passes, without walking at all. Returns `true` immediately when
    /// `NBP` disables pruning entirely.
    ///
    /// # Why `skip` exists
    ///
    /// Measured, this loop does almost no pruning: on B03 at the paper
    /// parameters **84-92% of every seed it consumed was consumed by buckets
    /// that went on to survive**. Junk buckets are rejected by the very first
    /// `sh` test, before a single `matches_in_bucket` call — a bucket seeded by
    /// one k-mer is already far past the miss budget. So the walk is not
    /// pruning cost. It is mostly the cost of driving `sh` all the way down to
    /// its final value on buckets whose fate was settled at the first test, and
    /// it is what makes a smaller `S` expensive: the seeds seeding no longer
    /// covers are exactly the ones this then has to walk.
    ///
    /// When the walk is worth doing anyway, and when it is not, is decided by
    /// the caller — see `prune_skip_enabled` in `scoring` for the measurement
    /// behind it.
    ///
    /// Skipping is safe wherever `find_best_mapping` does not read the bucket
    /// accumulator — i.e. `Containment`/`Jaccard`, where scoring is a pure
    /// function of the bucket's location — because the only consequence is that
    /// a bucket which *would* have been pruned gets scored instead, and such a
    /// bucket has `score <= sh < thr`, so it cannot be admitted. The answer is
    /// identical; only `refined_buckets` moves.
    ///
    /// What is not safe is reporting the truncated `sh`, which is then an upper
    /// bound rather than the value. [`SHMapper::complete_sh`] finishes the walk
    /// for the one or two buckets a read actually reports.
    // Innermost loop of the mapper. Bundling these into a context struct adds a
    // level of indirection to reach `bucket` and `sh`, and this workload is
    // memory-latency bound — the same change has been measured as a net loss
    // twice here. (The RESULTS.md section this used to cite no longer carries
    // that measurement — found stale while investigating Q7 in QUESTIONS.md —
    // so the pointer is dropped rather than left dangling; the finding itself
    // stands.)
    #[allow(clippy::too_many_arguments)]
    pub fn seed_heuristic_pass(
        &self,
        buckets: &Buckets<'idx, AP>,
        p_unique: &Seeds,
        m: QPos,
        b: &BucketLoc,
        bucket: &mut BucketContent,
        sh: &mut f64,
        thr: f64,
        skip: bool,
    ) -> bool {
        if !NBP {
            loop {
                *sh = self.hseed(m, bucket.seeds, bucket.matches);
                if *sh < thr {
                    return false;
                }
                if (bucket.i as usize) >= p_unique.len() || skip {
                    break;
                }
                self.matches_in_bucket(buckets, b, bucket, &p_unique[bucket.i as usize]);
                bucket.i += 1;
            }
        }
        true
    }

    /// Walks `bucket` to the end of `p_unique` regardless of `sh`, and returns
    /// the completed seed-heuristic score.
    ///
    /// The counterpart to `seed_heuristic_pass`'s `skip`: a reported mapping's
    /// `sh:f:` tag has to be the real thing, so whichever one or two buckets a
    /// read reports get finished here rather than every candidate getting
    /// finished during the sweep. Idempotent — a bucket already at the end
    /// returns its `sh` without touching anything.
    pub fn complete_sh(
        &self,
        buckets: &Buckets<'idx, AP>,
        p_unique: &Seeds,
        m: QPos,
        b: &BucketLoc,
        bucket: &mut BucketContent,
    ) -> f64 {
        if NBP {
            // `-P` never walks a bucket at all, so there is nothing to
            // complete: `seed_heuristic_pass` returns before assigning `sh`
            // and the sweep reports the 1.0 it was initialized to. Computing
            // an `hseed` here instead would make `-P`'s `sh:f:` tag a function
            // of `S`, which is exactly what this must not do.
            return 1.0;
        }
        while (bucket.i as usize) < p_unique.len() {
            self.matches_in_bucket(buckets, b, bucket, &p_unique[bucket.i as usize]);
            bucket.i += 1;
        }
        self.hseed(m, bucket.seeds, bucket.matches)
    }
}
