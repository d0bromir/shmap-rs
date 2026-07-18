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
            let hit = self.tidx.h2single[&s.kmer.h];
            let in_range = if AP {
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
            let hits = &self.tidx.h2multi[&s.kmer.h];
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
    /// moment it can't (bucket is prunable), `true` if it survives to the
    /// end of `p_unique` (or immediately, when `NBP` disables pruning
    /// entirely).
    pub fn seed_heuristic_pass(
        &self,
        buckets: &Buckets<'idx, AP>,
        p_unique: &Seeds,
        m: QPos,
        b: &BucketLoc,
        bucket: &mut BucketContent,
        sh: &mut f64,
        thr: f64,
    ) -> bool {
        if !NBP {
            loop {
                *sh = self.hseed(m, bucket.seeds, bucket.matches);
                if *sh < thr {
                    return false;
                }
                if (bucket.i as usize) >= p_unique.len() {
                    break;
                }
                self.matches_in_bucket(buckets, b, bucket, &p_unique[bucket.i as usize]);
                bucket.i += 1;
            }
        }
        true
    }
}
