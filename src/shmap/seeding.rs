//! `unique_elements_with_info`, `more_seeds_if_cheap`, `match_seeds`.

use super::SHMapper;
use crate::buckets::Buckets;
use crate::types::{BucketContent, BucketLoc, Kmer, PMatches, QPos, RPos, Seed, Seeds, SegmId};

/// Emits one finished bucket accumulator from [`SHMapper::match_seeds`]'s
/// streaming multi-hit path, clamping its match count to how many times the
/// seed's k-mer actually occurs in the read.
#[inline]
fn flush_slot<const AP: bool>(
    buckets: &mut Buckets<'_, AP>,
    segm_id: SegmId,
    bb: RPos,
    acc: &BucketContent,
    occs: QPos,
) {
    let clamped = BucketContent::new(acc.matches.min(occs), 0, acc.codirection, acc.r_min, acc.r_max);
    buckets.add_to_bucket(BucketLoc::new(segm_id, bb), clamped);
}

impl<'idx, const NBP: bool, const OS: bool, const AP: bool> SHMapper<'idx, NBP, OS, AP> {
    /// Groups `p` by k-mer hash and returns one [`Seed`] per distinct
    /// hash, sorted ascending by `hits_in_t` (so seeding preferentially
    /// consumes the rarest — most discriminative — k-mers first).
    pub fn unique_elements_with_info(&mut self, p: &mut [Kmer]) -> Seeds {
        self.timers.start("group_kmers");
        p.sort_by(|a, b| {
            if a.h != b.h {
                a.h.cmp(&b.h)
            } else {
                b.r.cmp(&a.r) // reverse order of inclusion in the query; needed for LCS
            }
        });
        self.timers.stop("group_kmers");

        self.timers.start("collect_kmer_info");
        // Upper bound: one seed per k-mer, when every hash is distinct.
        let mut p_unique: Seeds = Vec::with_capacity(p.len());
        let mut nonzero: RPos = 0;
        // `p` is sorted by hash, so each distinct hash is one contiguous run.
        // Taking the run as a slice means the single-occurrence case (~96% of
        // k-mers on real reads) stores its position inline instead of heap-
        // allocating a one-element `Vec` per seed.
        let mut group_start = 0usize;
        for ppos in 0..p.len() {
            if ppos + 1 == p.len() || p[ppos].h != p[ppos + 1].h {
                let group = &p[group_start..=ppos];
                let strike = group.len() as QPos;
                let pmatches = match group {
                    [only] => PMatches::One(only.r),
                    _ => PMatches::Many(group.iter().map(|k| k.r).collect()),
                };
                let hits_in_t = self.tidx.count(p[ppos].h);
                let seed_num = p_unique.len() as QPos;
                p_unique.push(Seed::new(p[ppos], hits_in_t, strike, seed_num, pmatches));
                // Fixed vs. upstream: the C++ resets `strike` to 0 *before*
                // this check, so its `nonzero` always adds 0 and
                // `kmers_notmatched` always reports the entire sketch size.
                // Cosmetic/diagnostic-only; fixed per this port's
                // fix-real-bugs decision.
                if hits_in_t > 0 {
                    nonzero += strike;
                }
                group_start = ppos + 1;
            }
        }
        self.timers.stop("collect_kmer_info");

        self.timers.start("sort_kmers");
        p_unique.sort_by(|a, b| a.hits_in_t.cmp(&b.hits_in_t));
        self.timers.stop("sort_kmers");

        self.counters.inc("kmers_notmatched", p.len() as i64 - nonzero as i64);

        p_unique
    }

    /// Extends the seed count `S` while doing so is "free" (only k-mers
    /// with at most one reference hit). Ported for parity — the only call
    /// site upstream is commented out (`//S = more_seeds_if_cheap(S,
    /// p_unique);`), so this is currently unreachable from `map_read` here
    /// too, matching that.
    pub fn more_seeds_if_cheap(&self, s: QPos, p_unique: &Seeds, verbose: i32) -> QPos {
        let original_s = s;
        let mut s = s;
        while (s as usize) < p_unique.len() && p_unique[s as usize].hits_in_t <= 1 {
            s += 1;
        }
        if s > original_s && verbose >= 2 {
            eprintln!("Increased seeds from {original_s} to {s}");
        }
        s
    }

    /// Matches the first `S` seeds (by seed order, i.e. rarest-first)
    /// against the index, accumulating hit counts into `buckets`.
    pub fn match_seeds(&mut self, p_unique: &Seeds, buckets: &mut Buckets<'idx, AP>, s: QPos) {
        let mut seed_matches: RPos = 0;
        let halflen = buckets.halflen;
        while (buckets.i as usize) < p_unique.len() && buckets.seeds < s {
            let seed = &p_unique[buckets.i as usize];
            if seed.hits_in_t > 0 {
                seed_matches += seed.hits_in_t;
                self.counters.update_max("max_seed_matches", seed.hits_in_t as i64);

                if seed.hits_in_t == 1 {
                    let hit = self.tidx.h2single[&seed.kmer.h];
                    let content = BucketContent::new(
                        1,
                        0,
                        if hit.strand == seed.kmer.strand { 1 } else { -1 },
                        hit.r,
                        hit.r,
                    );
                    buckets.add_to_pos(&hit, content);
                } else {
                    // Streaming replacement for the per-seed `BucketsHash`:
                    // `h2multi[h]` is sorted by `(segm_id, r)`, and within a
                    // segment `r`/`tpos` increase together, so the bucket
                    // index `pos/halflen` is monotonically non-decreasing
                    // across this seed's hits. Each hit only touches buckets
                    // `b` and `b-1`, so a bucket is final once we reach a hit
                    // two buckets ahead. This streams the seed's hits into
                    // `buckets` directly with O(1) integer work per hit,
                    // instead of the ~O(hits) FxHashMap inserts the scratch
                    // `BucketsHash` did — `match_seeds`'s dominant cost on
                    // repetitive references (see `PROFILING.md`).
                    //
                    // The per-seed aggregation exists because the `min(occs)`
                    // clamp below applies to a whole bucket's match count, so
                    // it cannot be applied per hit.
                    //
                    // Output is unchanged: the same set of buckets receives
                    // the same accumulated (clamped) content, and
                    // `Buckets` re-derives its results in location order, so
                    // the order buckets are added in doesn't affect the result.
                    let occs = seed.occs_in_p;
                    let mut cur_sid: SegmId = -1;
                    // Bucket index of the current hit, tracked incrementally:
                    // `pos` is non-decreasing within a segment, so instead of
                    // a division `pos / halflen` per hit we advance `b` (and
                    // its upper bound `b_hi = (b+1)*halflen`) by comparison,
                    // recomputing by division only on a segment change. Over
                    // billions of hits this replaces a per-hit integer divide
                    // with an amortized-O(1) compare.
                    let mut b: RPos = 0;
                    let mut b_hi: RPos = 0;
                    // The only two buckets that can still receive a
                    // contribution, held as fixed slots for `base` and
                    // `base + 1`: a hit at bucket `b` touches `b` and `b - 1`,
                    // and `b` never decreases within a segment, so `base` is
                    // always `max(b - 1, 0)` and everything below it is final.
                    // A `matches` of 0 marks an empty slot, since every
                    // contribution carries `matches == 1`.
                    let mut base: RPos = 0;
                    let mut acc = [BucketContent::default(); 2];
                    for hit in &self.tidx.h2multi[&seed.kmer.h] {
                        let pos = if AP { hit.r } else { hit.tpos };
                        let content = BucketContent::new(
                            1,
                            0,
                            if hit.strand == seed.kmer.strand { 1 } else { -1 },
                            hit.r,
                            hit.r,
                        );
                        if hit.segm_id != cur_sid {
                            // New segment: everything buffered is final.
                            for (j, a) in acc.iter_mut().enumerate() {
                                if a.matches > 0 {
                                    flush_slot(buckets, cur_sid, base + j as RPos, a, occs);
                                    *a = BucketContent::default();
                                }
                            }
                            cur_sid = hit.segm_id;
                            b = pos / halflen;
                            b_hi = (b + 1) * halflen;
                            base = (b - 1).max(0);
                        } else {
                            // Advance `b` to the bucket containing `pos`
                            // (monotonic within a segment — no division).
                            while pos >= b_hi {
                                b += 1;
                                b_hi += halflen;
                            }
                            // Slide the two-slot window up to the new `base`,
                            // finalizing whatever falls out the bottom.
                            let new_base = (b - 1).max(0);
                            if new_base > base {
                                if acc[0].matches > 0 {
                                    flush_slot(buckets, cur_sid, base, &acc[0], occs);
                                }
                                if new_base == base + 1 {
                                    acc[0] = acc[1];
                                } else {
                                    if acc[1].matches > 0 {
                                        flush_slot(buckets, cur_sid, base + 1, &acc[1], occs);
                                    }
                                    acc[0] = BucketContent::default();
                                }
                                acc[1] = BucketContent::default();
                                base = new_base;
                            }
                        }
                        acc[(b - base) as usize] += content;
                        if b > 0 {
                            acc[(b - 1 - base) as usize] += content;
                        }
                    }
                    for (j, a) in acc.iter().enumerate() {
                        if a.matches > 0 {
                            flush_slot(buckets, cur_sid, base + j as RPos, a, occs);
                        }
                    }
                }
            }
            buckets.seeds += seed.occs_in_p;
            buckets.i += 1;
        }
        self.counters.inc("seed_matches", seed_matches as i64);
        self.counters.inc("total_matches", seed_matches as i64);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::index::SketchIndex;
    use std::collections::HashSet;

    #[test]
    fn unique_elements_groups_by_hash_and_collects_pmatches() {
        let tidx = SketchIndex::new();
        let mut mapper: SHMapper<false, false, false> = SHMapper::new(&tidx);

        let mut p = vec![
            Kmer::new(60, 0x111111, false),
            Kmer::new(70, 0x222222, false),
            Kmer::new(10, 0x111111, true),
            Kmer::new(20, 0x222222, true),
            Kmer::new(30, 0x111111, true),
            Kmer::new(40, 0x444444, false),
            Kmer::new(50, 0x555555, false),
        ];

        let pmatches_gt: HashSet<Vec<QPos>> =
            [vec![60, 30, 10], vec![70, 20], vec![40], vec![50]].into_iter().collect();

        let seeds = mapper.unique_elements_with_info(&mut p);
        let pmatches_res: HashSet<Vec<QPos>> = seeds.iter().map(|s| s.pmatches.to_vec()).collect();

        assert_eq!(pmatches_res.len(), pmatches_gt.len());
        assert_eq!(pmatches_res, pmatches_gt);
    }
}
