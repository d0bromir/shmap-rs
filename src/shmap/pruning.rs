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
    /// moment it can't (bucket is prunable), `true` if it survives to the
    /// end of `p_unique` (or immediately, when `NBP` disables pruning
    /// entirely).
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::index::SketchIndex;
    use crate::sketch::RefSegment;
    use crate::types::{Hit, Kmer};

    fn check_ranges<const AP: bool>(index: &SketchIndex) {
        let mapper = SHMapper::<false, false, AP>::new(index);
        let mut buckets = Buckets::<AP>::new(index);
        assert!(buckets.set_halflen(5, 5));
        for hash in [8, 16, 24] {
            for occurrences in [1, 3, 100] {
                for strand in [false, true] {
                    let seed = Seed::new(
                        Kmer::new(0, hash, strand),
                        index.count(hash),
                        occurrences,
                        0,
                        vec![0; occurrences as usize].into(),
                    );
                    for segment in 0..4 {
                        for position in 0..30 {
                            let location = BucketLoc::new(segment, position);
                            let mut actual = BucketContent::default();
                            mapper.matches_in_bucket(&buckets, &location, &mut actual, &seed);
                            let mut expected = BucketContent::default();
                            expected.seeds += occurrences;
                            for hit in index.hits(hash) {
                                let coordinate = if AP { hit.r } else { hit.tpos };
                                if hit.segm_id == segment
                                    && buckets.begin(&location) <= coordinate
                                    && coordinate < buckets.end(&location)
                                {
                                    expected.matches += 1;
                                    expected.codirection += if hit.strand == strand { 1 } else { -1 };
                                    expected.r_min = expected.r_min.min(hit.r);
                                    expected.r_max = expected.r_max.max(hit.r);
                                }
                            }
                            expected.matches = expected.matches.min(occurrences);
                            assert_eq!(actual.matches, expected.matches);
                            assert_eq!(actual.seeds, expected.seeds);
                            assert_eq!(actual.codirection, expected.codirection);
                            assert_eq!(actual.r_min, expected.r_min);
                            assert_eq!(actual.r_max, expected.r_max);
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn posting_ranges_match_full_scan() {
        let mut index = SketchIndex::new();
        for segment in 0..4 {
            index.segments.push(RefSegment::new(
                (0..150).map(|position| Kmer::new(position * 2, 8, false)).collect(),
                format!("ref{segment}"),
                300,
                0,
            ));
        }
        let hits: Vec<_> = [1, 2]
            .into_iter()
            .flat_map(|segment| {
                [0, 5, 9, 10, 15, 50, 99]
                    .into_iter()
                    .map(move |position| Hit::new(&Kmer::new(position * 2, 8, position % 2 == 0), position, segment))
            })
            .collect();
        index.shards[0].h2multi.insert(8, hits);
        index.shards[0]
            .h2single
            .insert(16, Hit::new(&Kmer::new(20, 16, false), 10, 2));
        check_ranges::<false>(&index);
        check_ranges::<true>(&index);
        index.compact();
        check_ranges::<false>(&index);
        check_ranges::<true>(&index);
    }

    #[test]
    fn coarse_containment_bounds_dominate_exact_windows() {
        use crate::types::{H2Seed, Metric};
        use std::collections::BTreeMap;

        let index = SketchIndex::new();
        let mapper = SHMapper::<false, false, false>::new(&index);
        let mut rejected = 0;
        let mut retained = 0;
        let mut halo_required = 0;
        for reference_mode in 0..3 {
            let segment = RefSegment::new(
                (0..180)
                    .map(|position| {
                        let hash = match reference_mode {
                            0 => position % 17,
                            1 => 8,
                            _ => (position / 9) % 17,
                        };
                        Kmer::new(position * 5 + 25, hash as u64, position % 2 == 0)
                    })
                    .collect(),
                "ref".into(),
                1000,
                0,
            );
            for hashes in [vec![8; 5], vec![0, 1, 2, 3, 4], vec![99; 5], vec![8, 8, 9, 9, 10]] {
                let mut counts = BTreeMap::new();
                for hash in &hashes {
                    *counts.entry(*hash).or_insert(0) += 1;
                }
                let mut query = H2Seed::default();
                let mut original_hist = Vec::new();
                for (number, (&hash, &count)) in counts.iter().enumerate() {
                    query.insert(
                        hash,
                        Seed::new(
                            Kmer::new(25, hash, false),
                            0,
                            count,
                            number as QPos,
                            vec![0; count as usize].into(),
                        ),
                    );
                    original_hist.push(count);
                }
                let window = hashes.len();
                let bound = |begin: usize, end: usize| {
                    counts
                        .iter()
                        .map(|(&hash, &count)| {
                            count.min(segment.kmers[begin..end].iter().filter(|kmer| kmer.h == hash).count() as QPos)
                        })
                        .sum::<QPos>() as f64
                        / window as f64
                };
                for block_width in [1, 7, 16, 64] {
                    for block_start in (0..segment.kmers.len()).step_by(block_width) {
                        let starts_end = (block_start + block_width).min(segment.kmers.len());
                        let containing_end = (starts_end + 2 * window - 1).min(segment.kmers.len());
                        let upper = bound(block_start, containing_end);
                        if upper < 0.4 {
                            rejected += 1;
                        } else {
                            retained += 1;
                        }
                        for start in block_start..starts_end {
                            let mut histogram = original_hist.clone();
                            let exact = mapper.best_fixed_length(
                                &segment,
                                start as RPos,
                                (start + 2 * window).min(segment.kmers.len()) as RPos,
                                &query,
                                &mut histogram,
                                100,
                                window as QPos,
                                Metric::Containment,
                                None,
                                0.3,
                            );
                            assert!(exact.score() <= upper);
                            assert_eq!(histogram, original_hist);
                            halo_required += usize::from(exact.score() > bound(block_start, starts_end));
                        }
                    }
                }
            }
        }
        assert!(rejected > 0 && retained > 0 && halo_required > 0);
        eprintln!(
            "shadow bound: {rejected} rejected, {retained} retained regions; {halo_required} windows require the halo"
        );
    }
}
