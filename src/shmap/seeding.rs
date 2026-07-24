//! `unique_elements_with_info`, `more_seeds_if_cheap`, `match_seeds`.

use super::SHMapper;
use crate::buckets::Buckets;
use crate::types::{BucketContent, BucketLoc, Kmer, QPos, RPos, Seed, Seeds, SegmId};

/// Accumulates `content` into the entry for bucket index `bb` in the tiny,
/// ascending-by-index scratch buffer used by [`SHMapper::match_seeds`]'s
/// streaming multi-hit path. The buffer holds at most a couple of live
/// buckets (see that method), so the linear scan/insert is effectively O(1).
fn buf_add(buf: &mut Vec<(RPos, BucketContent)>, bb: RPos, content: BucketContent) {
    for i in 0..buf.len() {
        if buf[i].0 == bb {
            buf[i].1 += content;
            return;
        }
        if buf[i].0 > bb {
            buf.insert(i, (bb, content));
            return;
        }
    }
    buf.push((bb, content));
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
        let mut p_unique: Seeds = Vec::new();
        let mut strike: QPos = 0;
        let mut nonzero: RPos = 0;
        let mut matches: Vec<QPos> = Vec::new();
        for ppos in 0..p.len() {
            strike += 1;
            matches.push(p[ppos].r);
            if ppos == p.len() - 1 || p[ppos].h != p[ppos + 1].h {
                let hits_in_t = self.tidx.count(p[ppos].h);
                let seed_num = p_unique.len() as QPos;
                p_unique.push(Seed::new(p[ppos], hits_in_t, strike, seed_num, matches.clone()));
                // Fixed vs. upstream: the C++ resets `strike` to 0 *before*
                // this check (`strike = 0;` appears above the
                // `if (hits_in_t > 0) nonzero += strike;` line there), so
                // `nonzero` there always adds 0 regardless of match count,
                // and `kmers_notmatched` always reports the *entire*
                // sketch size. Cosmetic/diagnostic-only (never read by the
                // mapping algorithm or written into a PAF tag) — fixed per
                // this port's general fix-real-bugs decision, not asked
                // about separately since it's strictly lower-stakes than
                // the counter-reset bug that decision was made for.
                if hits_in_t > 0 {
                    nonzero += strike;
                }
                strike = 0;
                matches.clear();
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
        // Reused across seeds. Holds the currently-"active" buckets for the
        // streaming multi-hit aggregation below (at most a couple live at a
        // time), ascending by bucket index.
        let mut stream_buf: Vec<(RPos, BucketContent)> = Vec::new();
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
                    // two buckets ahead (`bb < b-1`). This streams the seed's
                    // hits into `buckets` directly with O(1) integer work per
                    // hit, instead of the ~O(hits) FxHashMap inserts the
                    // scratch `BucketsHash` did — `match_seeds`'s dominant
                    // cost on repetitive references (see `PROFILING.md`).
                    //
                    // Output is unchanged: the same set of buckets receives
                    // the same accumulated (clamped) content, and
                    // `Buckets::get_sorted_buckets` re-sorts by location +
                    // stable-sorts by match count, so the order buckets are
                    // added in doesn't affect the result.
                    let occs = seed.occs_in_p;
                    stream_buf.clear();
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
                            for (bb, c) in stream_buf.drain(..) {
                                let clamped =
                                    BucketContent::new(c.matches.min(occs), 0, c.codirection, c.r_min, c.r_max);
                                buckets.add_to_bucket(BucketLoc::new(cur_sid, bb), clamped);
                            }
                            cur_sid = hit.segm_id;
                            b = pos / halflen;
                            b_hi = (b + 1) * halflen;
                        } else {
                            // Advance `b` to the bucket containing `pos`
                            // (monotonic within a segment — no division).
                            while pos >= b_hi {
                                b += 1;
                                b_hi += halflen;
                            }
                            // Finalize buckets that can receive no further
                            // contribution (index strictly below `b - 1`).
                            while let Some(&(bb, c)) = stream_buf.first() {
                                if bb < b - 1 {
                                    let clamped = BucketContent::new(
                                        c.matches.min(occs),
                                        0,
                                        c.codirection,
                                        c.r_min,
                                        c.r_max,
                                    );
                                    buckets.add_to_bucket(BucketLoc::new(cur_sid, bb), clamped);
                                    stream_buf.remove(0);
                                } else {
                                    break;
                                }
                            }
                        }
                        buf_add(&mut stream_buf, b, content);
                        if b > 0 {
                            buf_add(&mut stream_buf, b - 1, content);
                        }
                    }
                    for (bb, c) in stream_buf.drain(..) {
                        let clamped =
                            BucketContent::new(c.matches.min(occs), 0, c.codirection, c.r_min, c.r_max);
                        buckets.add_to_bucket(BucketLoc::new(cur_sid, bb), clamped);
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
        let pmatches_res: HashSet<Vec<QPos>> = seeds.iter().map(|s| s.pmatches.clone()).collect();

        assert_eq!(pmatches_res.len(), pmatches_gt.len());
        assert_eq!(pmatches_res, pmatches_gt);
    }
}
