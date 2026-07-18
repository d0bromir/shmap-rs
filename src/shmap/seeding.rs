//! `unique_elements_with_info`, `more_seeds_if_cheap`, `match_seeds`.

use super::SHMapper;
use crate::buckets::{Buckets, BucketsHash};
use crate::types::{BucketContent, Kmer, QPos, RPos, Seed, Seeds};

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
                    let mut b2m: BucketsHash<AP> = BucketsHash::new(buckets.halflen);
                    for hit in &self.tidx.h2multi[&seed.kmer.h] {
                        let content = BucketContent::new(
                            1,
                            0,
                            if hit.strand == seed.kmer.strand { 1 } else { -1 },
                            hit.r,
                            hit.r,
                        );
                        b2m.add_to_pos(hit, content);
                    }
                    for (loc, content) in b2m.buckets.iter() {
                        let clamped = BucketContent::new(
                            content.matches.min(seed.occs_in_p),
                            0,
                            content.codirection,
                            content.r_min,
                            content.r_max,
                        );
                        buckets.add_to_bucket(*loc, clamped);
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
