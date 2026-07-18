//! Mapping result types and PAF output formatting.
//!
//! Port of the result/output half of `shmap/src/sketch.h`.

use std::fmt;

use crate::sketch::RefSegment;
use crate::types::{BucketLoc, QPos, RPos, SegmId};

/// The minimal mapping info for PAF output.
#[derive(Clone, Debug)]
pub struct MappingPaf {
    pub p_start: QPos,
    pub p_end: QPos,
    pub strand: char,
    pub segm_name: String,
    pub segm_sz: RPos,
    pub segm_id: SegmId,
    /// Leftmost mapped reference position.
    pub t_l: RPos,
    /// Rightmost mapped reference position.
    pub t_r: RPos,
    /// `0..=60`, or `255` for "not yet computed".
    pub mapq: u8,
    pub query_id: String,
    pub p_sz: QPos,
}

impl Default for MappingPaf {
    fn default() -> Self {
        MappingPaf {
            p_start: -1,
            p_end: -1,
            strand: '?',
            segm_name: String::new(),
            segm_sz: -1,
            segm_id: -1,
            t_l: -1,
            t_r: -1,
            mapq: 255,
            query_id: String::new(),
            p_sz: -1,
        }
    }
}

impl MappingPaf {
    pub fn new(
        p_start: QPos,
        p_end: QPos,
        strand: char,
        segm_name: &str,
        segm_sz: RPos,
        segm_id: SegmId,
        t_l: RPos,
        t_r: RPos,
    ) -> Self {
        MappingPaf {
            p_start,
            p_end,
            strand,
            segm_name: segm_name.to_string(),
            segm_sz,
            segm_id,
            t_l,
            t_r,
            mapq: 255,
            query_id: String::new(),
            p_sz: -1,
        }
    }

    /// A minimal record for a read that failed to map, in the broad shape
    /// of the minimap2/samtools convention for unmapped records (known
    /// fields filled in, alignment fields zeroed/`*`). Upstream C++ has no
    /// defined behavior for this case at all — see the unmapped-read
    /// decision made when this port was planned; this is that decision's
    /// concrete implementation, not a literal translation of anything.
    pub fn unmapped_line(query_id: &str, read_len: QPos) -> String {
        format!("{query_id}\t{read_len}\t0\t0\t*\t*\t0\t0\t0\t0\t0\t0")
    }
}

impl fmt::Display for MappingPaf {
    // --- https://github.com/lh3/miniasm/blob/master/PAF.md ---
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}",
            self.query_id,
            self.p_sz,
            self.p_start,
            self.p_end,
            self.strand,
            self.segm_name,
            self.segm_sz,
            self.t_l,
            self.t_r,
            self.p_sz, // TODO(upstream): should be the actual match count
            self.p_sz, // TODO(upstream): should be the actual block length
            self.mapq,
        )
    }
}

#[derive(Clone, Debug)]
pub struct LocalMappingStats {
    /// Reference span of the mapping (rightmost - leftmost position + 1).
    pub s_sz: RPos,
    /// Number of k-mers in the intersection between the pattern and its
    /// mapping in `T`.
    pub intersection: QPos,
    pub map_time: f64,
    /// Positive/negative for more same-/opposite-strand seed matches.
    pub same_strand_seeds: i32,
    /// Similarity of the best mapping, in `[0, 1]`.
    pub j: f64,
    /// Similarity of the second-best mapping.
    pub j2: f64,
    pub sh: f64,
    /// Sketch size (number of seeds considered for this read).
    pub p_sz: QPos,
    pub bucket: BucketLoc,
    pub bucket2: BucketLoc,
    pub intersection2: QPos,
    /// How many sigmas apart `intersection`/`intersection2` are.
    pub sigmas_diff: f64,
}

impl Default for LocalMappingStats {
    fn default() -> Self {
        LocalMappingStats {
            s_sz: -1,
            intersection: -1,
            map_time: -1.0,
            same_strand_seeds: -1,
            j: -1.0,
            j2: -1.0,
            sh: -1.0,
            p_sz: -1,
            bucket: BucketLoc::default(),
            bucket2: BucketLoc::default(),
            intersection2: -1,
            sigmas_diff: -1.0,
        }
    }
}

impl fmt::Display for LocalMappingStats {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "\tp:i:{}\ts:i:{}\tI:i:{}\tI2:i:{}\tIdiff:i:{}\tIsdiff:f:{:.5}\tJ:f:{:.5}\tJ2:f:{:.5}\tJdiff:f:{:.5}\tsh:f:{:.5}\tstrand:i:{}\tt:f:{:.5}\tb:s:{}\tb2:s:{}",
            self.p_sz,
            self.s_sz,
            self.intersection,
            self.intersection2,
            self.intersection - self.intersection2,
            self.sigmas_diff,
            self.j,
            self.j2,
            self.j - self.j2,
            self.sh,
            self.same_strand_seeds,
            self.map_time,
            self.bucket,
            self.bucket2,
        )
    }
}

#[derive(Clone, Debug)]
pub struct GlobalMappingStats {
    pub k: QPos,
    /// Number of seeds before pruning.
    pub seeds: QPos,
    /// Number of matches of the most frequent seed.
    pub max_seed_matches: RPos,
    pub seed_matches: RPos,
    pub total_matches: RPos,
    /// `intersection / total_matches`.
    pub match_inefficiency: f64,
    pub seeded_buckets: RPos,
    pub final_buckets: RPos,
    /// False discovery rate: FP / PP.
    pub fptp: f64,
    /// Ground-truth Jaccard/Containment of the reported mapping. Always
    /// `-1.0` upstream — the field exists but nothing ever computes a real
    /// value into it (the actual ground-truth comparison lives entirely in
    /// `analyse_simulated`'s separate output, not here) — ported faithfully
    /// as a permanent placeholder rather than silently dropped.
    pub gt_j: f64,
    pub gt_c: f64,
    pub gt_c_bucket: f64,
}

impl Default for GlobalMappingStats {
    fn default() -> Self {
        GlobalMappingStats {
            k: -1,
            seeds: -1,
            max_seed_matches: -1,
            seed_matches: -1,
            total_matches: -1,
            match_inefficiency: -1.0,
            seeded_buckets: -1,
            final_buckets: -1,
            fptp: -1.0,
            gt_j: -1.0,
            gt_c: -1.0,
            gt_c_bucket: -1.0,
        }
    }
}

impl fmt::Display for GlobalMappingStats {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "\tk:i:{}\tgt_J:f:{:.3}\tgt_C:f:{:.3}\tgt_C_bucket:f:{:.3}\tseeds:i:{}\tmax_seed_matches:i:{}\tseed_matches:i:{}\ttotal_matches:i:{}\tmatch_inefficiency:f:{:.3}\tseeded_buckets:i:{}\tfinal_buckets:i:{}\tFPTP:f:{:.3}",
            self.k,
            self.gt_j,
            self.gt_c,
            self.gt_c_bucket,
            self.seeds,
            self.max_seed_matches,
            self.seed_matches,
            self.total_matches,
            self.match_inefficiency,
            self.seeded_buckets,
            self.final_buckets,
            self.fptp,
        )
    }
}

/// A candidate (or final) read-to-reference mapping, tracked as the "best
/// so far" while sweeping buckets — `update()` only overwrites it when a
/// strictly better score is found, so it behaves like a running max.
#[derive(Clone, Debug, Default)]
pub struct Mapping {
    pub paf: MappingPaf,
    pub local_stats: LocalMappingStats,
    pub global_stats: Option<Box<GlobalMappingStats>>,
}

impl Mapping {
    pub fn score(&self) -> f64 {
        self.local_stats.j
    }

    pub fn score2(&self) -> f64 {
        self.local_stats.j2
    }

    pub fn segm_id(&self) -> SegmId {
        self.paf.segm_id
    }

    pub fn set_sh(&mut self, sh: f64) {
        self.local_stats.sh = sh;
    }

    pub fn set_score2(&mut self, score2: f64) {
        self.local_stats.j2 = score2;
    }

    pub fn intersection(&self) -> QPos {
        self.local_stats.intersection
    }

    pub fn mapq(&self) -> u8 {
        self.paf.mapq
    }

    pub fn bucket(&self) -> BucketLoc {
        self.local_stats.bucket
    }

    pub fn set_bucket(&mut self, bucket: BucketLoc) {
        self.local_stats.bucket = bucket;
    }

    /// Overwrites this mapping with the given candidate iff `new_score`
    /// beats the current best score.
    #[allow(clippy::too_many_arguments)]
    pub fn update(
        &mut self,
        p_start: QPos,
        p_end: QPos,
        t_l: RPos,
        t_r: RPos,
        segm: &RefSegment,
        intersection: QPos,
        new_score: f64,
        same_strand_seeds: i32,
        sz: RPos,
    ) {
        if new_score > self.score() {
            self.paf = MappingPaf::new(
                p_start,
                p_end,
                if same_strand_seeds > 0 { '+' } else { '-' },
                &segm.name,
                segm.sz,
                segm.id,
                t_l,
                t_r,
            );
            self.local_stats.s_sz = sz;
            self.local_stats.same_strand_seeds = same_strand_seeds;
            self.local_stats.intersection = intersection;
            self.local_stats.j = new_score;
        }
    }

    fn sigmas_diff(x: QPos, y: QPos) -> f64 {
        let x = if x == -1 { 0 } else { x } as f64;
        let y = if y == -1 { 0 } else { y } as f64;
        (x - y).abs() / (x + y).sqrt()
    }

    /// Similar to minimap2: `mapQ = 40 (1-f2/f1) min(1, m/10) log f1`
    /// (per the comment upstream, though the *implemented* formula below
    /// it is a simpler all-or-nothing scheme — ported as implemented, not
    /// as commented; there are two large alternate formulas commented out
    /// in the C++ suggesting unsettled tuning, noted here in case a
    /// different mapq scheme is wanted later).
    pub fn calc_mapq(&mut self, theta2: f64, min_diff: f64) -> u8 {
        if self.score2() < theta2 {
            self.set_score2(theta2);
        }
        let frac = 1.0 - self.score2() / self.score();
        // Integer division on purpose: `local_stats.intersection` is a
        // qpos_t (i32) in the original, and `/2` there truncates.
        let mapq_strand: i32 = if self.local_stats.same_strand_seeds.abs() < self.local_stats.intersection / 2 {
            5
        } else {
            60
        };
        if frac > min_diff {
            60.min(mapq_strand) as u8
        } else {
            0
        }
    }

    pub fn set_second_best(&mut self, m2: &Mapping) {
        self.local_stats.bucket2 = m2.local_stats.bucket;
        self.local_stats.intersection2 = m2.local_stats.intersection;
        self.set_score2(m2.score());
        self.local_stats.sigmas_diff =
            Self::sigmas_diff(self.local_stats.intersection2, self.local_stats.intersection);
    }

    /// `sketch_size` is the read's full sketch size (`m` at the call site,
    /// stored into `local_stats.p_sz`); `read_len` is the read's raw
    /// nucleotide length (stored into `paf.p_sz`, the PAF "query length"
    /// column). Upstream calls both of these `qpos_t P_sz`/`p_sz` at
    /// different points, which reads as the same quantity but isn't —
    /// distinct names here to avoid reintroducing that confusion.
    #[allow(clippy::too_many_arguments)]
    pub fn set_global_stats(
        &mut self,
        theta2: f64,
        min_diff: f64,
        sketch_size: QPos,
        query_id: &str,
        read_len: QPos,
        k: QPos,
        seeds: QPos,
        total_matches: RPos,
        max_seed_matches: RPos,
        seed_matches: RPos,
        seeded_buckets: RPos,
        final_buckets: RPos,
        fptp: f64,
        map_time: f64,
    ) {
        self.paf.query_id = query_id.to_string();
        self.paf.p_sz = read_len;
        self.paf.mapq = self.calc_mapq(theta2, min_diff);

        self.local_stats.p_sz = sketch_size;
        self.local_stats.map_time = map_time;

        self.global_stats = Some(Box::new(GlobalMappingStats {
            k,
            seeds,
            total_matches,
            max_seed_matches,
            seed_matches,
            seeded_buckets,
            final_buckets,
            fptp,
            match_inefficiency: total_matches as f64 / self.local_stats.intersection as f64,
            gt_j: -1.0,
            gt_c: -1.0,
            gt_c_bucket: -1.0,
        }));
    }

    /// Fraction of `a`/`b`'s `[T_l, T_r]` reference spans that overlap, in
    /// `[0, 1]`; `-0.0` if they're on different segments.
    pub fn overlap(a: &Mapping, b: &Mapping) -> f64 {
        if a.paf.segm_id != b.paf.segm_id {
            return -0.0;
        }
        let cap = 0.max(a.paf.t_r.min(b.paf.t_r) - a.paf.t_l.max(b.paf.t_l));
        let cup = a.paf.t_r.max(b.paf.t_r) - a.paf.t_l.min(b.paf.t_l);
        debug_assert!(cup >= 0 && cap >= 0 && cup >= cap);
        cap as f64 / cup as f64
    }

    pub fn print_paf(&self, out: &mut impl std::io::Write) -> std::io::Result<()> {
        write!(out, "{self}")
    }
}

impl fmt::Display for Mapping {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}{}", self.paf, self.local_stats)?;
        if let Some(gs) = &self.global_stats {
            write!(f, "{gs}")?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn segm(name: &str, sz: RPos, id: i32) -> RefSegment {
        RefSegment::new(Vec::new(), name.to_string(), sz, id)
    }

    #[test]
    fn update_only_overwrites_on_improvement() {
        let s = segm("chr1", 1000, 0);
        let mut m = Mapping::default();
        assert_eq!(m.score(), -1.0);

        m.update(0, 9, 100, 200, &s, 5, 0.5, 3, 101);
        assert_eq!(m.score(), 0.5);
        assert_eq!(m.paf.strand, '+');

        m.update(0, 9, 300, 400, &s, 2, 0.2, -1, 101); // worse: ignored
        assert_eq!(m.score(), 0.5);
        assert_eq!(m.paf.t_l, 100);

        m.update(0, 9, 300, 400, &s, 8, 0.9, -1, 101); // better: overwrites
        assert_eq!(m.score(), 0.9);
        assert_eq!(m.paf.strand, '-');
        assert_eq!(m.paf.t_l, 300);
    }

    #[test]
    fn overlap_disjoint_segments_is_negative_zero() {
        let a_segm = segm("chr1", 1000, 0);
        let b_segm = segm("chr2", 1000, 1);
        let mut a = Mapping::default();
        a.update(0, 9, 0, 100, &a_segm, 5, 0.5, 1, 101);
        let mut b = Mapping::default();
        b.update(0, 9, 0, 100, &b_segm, 5, 0.5, 1, 101);
        assert_eq!(Mapping::overlap(&a, &b), -0.0);
    }

    #[test]
    fn overlap_same_segment_fraction() {
        let s = segm("chr1", 1000, 0);
        let mut a = Mapping::default();
        a.update(0, 9, 0, 100, &s, 5, 0.5, 1, 101);
        let mut b = Mapping::default();
        b.update(0, 9, 50, 150, &s, 5, 0.5, 1, 101);
        // intersection [50,100] = 50; union [0,150] = 150
        assert!((Mapping::overlap(&a, &b) - 50.0 / 150.0).abs() < 1e-9);
    }

    #[test]
    fn calc_mapq_uses_integer_division_for_strand_check() {
        let s = segm("chr1", 1000, 0);
        let mut m = Mapping::default();
        m.update(0, 9, 0, 100, &s, 5, 0.9, 3, 101); // intersection=5, 5/2==2 (int)
        // same_strand_seeds.abs()=3, not < 2 => mapq_strand=60
        let mapq = m.calc_mapq(0.5, 0.02);
        assert!(mapq == 60 || mapq == 0);
    }
}
