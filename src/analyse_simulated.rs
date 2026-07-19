//! Ground-truth comparison for simulated reads (verbose/`-z` mode).
//!
//! Port of `shmap/src/analyse_simulated.h`. This module has zero existing
//! test coverage upstream (no `TEST_CASE` in `test_shmap.cpp` references
//! it), so the fresh test below is this port's first real verification of
//! these semantics — treat it with the same extra scrutiny as
//! `buckets.rs`'s fresh coverage.
//!
//! `gt_c_r_lmax` is a confirmed-dead field upstream: it's declared and
//! printed in both `print_tsv`/`print_paf`, but never actually assigned in
//! the constructor (only `gt_C_l_lmax` is; there's no corresponding
//! `gt_C_r_lmax = ...` line — confirmed via grep). It always reports the
//! default `Mapping`'s score (`-1.000`). Unlike the per-read Counters
//! reset bug, this is purely a diagnostic-output quirk with zero existing
//! test coverage to tell us what was actually intended — left unassigned
//! here too rather than guessing a fix for a field nobody has exercised.

use std::io::Write;

use crate::buckets::Buckets;
use crate::index::SketchIndex;
use crate::mapping::Mapping;
use crate::refine::{Matcher, MappingMetric};
use crate::types::{BucketLoc, H2Cnt, H2Seed, QPos, RPos, SegmId};
use crate::utils::ParsedQueryId;

/// Header row for the `paul.tsv` file. Kept separate from
/// [`AnalyseSimulatedReads::render_tsv_row`] so callers can decide for
/// themselves when to emit it — the multithreaded mapping pipeline renders
/// rows on worker threads but writes (and headers) them from a single
/// serial collector, so it can no longer track "is this the first row" via
/// a `&mut bool` threaded through the render call itself.
pub const TSV_HEADER: &str =
    "query_id\tm\ttheta\thl\tsegm\tgt_l_bucket\tgt_r_bucket\tgt_next_bucket\tgt_J_l\tgt_J_r\tgt_J_next\tgt_C_l\tgt_C_r\tgt_C_next\tgt_C_l_lmax\tgt_C_r_lmax\t#J>theta\t#C>theta\tJ>theta\tC>theta\tmaxJ\tmaxC\tP";

pub struct AnalyseSimulatedReads<'idx, 'b, 'p, const AP: bool> {
    matcher: Matcher<'idx, 'b, AP>,
    p_ht: &'p H2Seed,
    tidx: &'idx SketchIndex,

    query_id: String,
    /// The read's own nucleotide sequence — stored only for `print_tsv`'s
    /// trailing `P` column.
    p: String,
    p_sz: QPos,
    m: QPos,
    theta: f64,
    bucket_l: QPos,

    pub gt_start_nucl: RPos,
    pub gt_end_nucl: RPos,
    pub start: RPos,
    pub end: RPos,
    pub segm_id: SegmId,
    pub segm_name: String,

    pub gt_b_l: BucketLoc,
    pub gt_b_r: BucketLoc,
    pub gt_b_next: BucketLoc,

    pub gt_mapping: Mapping,
    pub gt_j_l: Mapping,
    pub gt_j_r: Mapping,
    pub gt_j_next: Mapping,
    pub gt_c_l: Mapping,
    pub gt_c_r: Mapping,
    pub gt_c_next: Mapping,
    pub gt_c_l_lmax: Mapping,
    /// See the module doc comment: confirmed dead/unassigned upstream too.
    pub gt_c_r_lmax: Mapping,

    pub j_buckets: Vec<BucketLoc>,
    pub c_buckets: Vec<BucketLoc>,
}

impl<'idx, 'b, 'p, const AP: bool> AnalyseSimulatedReads<'idx, 'b, 'p, AP> {
    /// `query_id` must be ground-truth-encoded (e.g.
    /// `"S1_21!NC_060948.1!57693539!57715501!+"`) — matches the C++'s own
    /// `assert(parsed.valid)`, panicking otherwise, since this type only
    /// ever makes sense for simulated reads with that naming convention.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        query_id: &str,
        p: &[u8],
        p_sz: QPos,
        diff_hist: H2Cnt,
        m: QPos,
        p_ht: &'p H2Seed,
        tidx: &'idx SketchIndex,
        buckets: &'b mut Buckets<'idx, AP>,
        theta: f64,
    ) -> Self {
        let bucket_l = buckets.halflen;
        // The C++ takes `Buckets &B` (non-const) purely to call its
        // non-const `get_sorted_buckets()` once here; nothing else in this
        // type ever mutates it. Extract the sorted bucket locations while
        // we still have unique access, then downgrade to a shared borrow
        // for everything else (including the `Matcher` field, which only
        // ever needs `&Buckets`).
        let sorted_locs: Vec<BucketLoc> = buckets.get_sorted_buckets().into_iter().map(|(loc, _)| loc).collect();
        let buckets: &'b Buckets<'idx, AP> = buckets;
        let mut matcher = Matcher::new(tidx, buckets, diff_hist);

        let parsed_orig = ParsedQueryId::parse(query_id).expect("query_id must be ground-truth-encoded");
        let segm = tidx.get_segment_by_name(&parsed_orig.segm_id).expect("ground-truth segment not found in index");
        let mut gt_mapping = Mapping::default();
        gt_mapping.paf = crate::mapping::MappingPaf::new(
            0,
            p_sz,
            parsed_orig.strand,
            &segm.name,
            segm.sz,
            segm.id,
            parsed_orig.start_pos,
            parsed_orig.end_pos,
        );

        let (gt_start_nucl, gt_end_nucl, start, end, segm_id, segm_name) = Self::gt_start_end(tidx, query_id);

        let gt_b_l = BucketLoc::new(segm_id, 0.max(start / bucket_l - 1));
        let gt_b_r = BucketLoc::new(segm_id, start / bucket_l);
        let gt_b_next = BucketLoc::new(segm_id, start / bucket_l + 1);

        let gt_m_l = matcher.collect_matches(&gt_b_l, p_ht);
        let gt_m_r = matcher.collect_matches(&gt_b_r, p_ht);
        let gt_m_next = matcher.collect_matches(&gt_b_next, p_ht);

        let gt_j_l = matcher.best_included_jaccard(&gt_m_l, p_sz, end - start - 2, end - start + 2, m);
        let gt_j_r = matcher.best_included_jaccard(&gt_m_r, p_sz, end - start - 2, end - start + 2, m);
        let gt_j_next = matcher.best_included_jaccard(&gt_m_next, p_sz, end - start - 2, end - start + 2, m);
        let gt_c_l = matcher.best_fixed_length(&gt_m_l, p_sz, m, MappingMetric::ContainmentIndex);
        let gt_c_r = matcher.best_fixed_length(&gt_m_r, p_sz, m, MappingMetric::ContainmentIndex);
        let gt_c_next = matcher.best_fixed_length(&gt_m_next, p_sz, m, MappingMetric::ContainmentIndex);
        let gt_c_l_lmax = matcher.best_fixed_length(&gt_m_l, p_sz, m, MappingMetric::ContainmentIndex);

        let mut j_buckets = Vec::new();
        let mut c_buckets = Vec::new();
        for b in &sorted_locs {
            let m_slice = matcher.collect_matches(b, p_ht);
            let mapping_j = matcher.best_included_jaccard(&m_slice, p_sz, end - start - 2, end - start + 2, m);
            let mapping_c = matcher.best_fixed_length(&m_slice, p_sz, m, MappingMetric::ContainmentIndex);
            if mapping_j.score() >= theta {
                j_buckets.push(*b);
            }
            if mapping_c.score() >= theta {
                c_buckets.push(*b);
            }
        }

        AnalyseSimulatedReads {
            matcher,
            p_ht,
            tidx,
            query_id: query_id.to_string(),
            p: String::from_utf8_lossy(p).into_owned(),
            p_sz,
            m,
            theta,
            bucket_l,
            gt_start_nucl,
            gt_end_nucl,
            start,
            end,
            segm_id,
            segm_name,
            gt_b_l,
            gt_b_r,
            gt_b_next,
            gt_mapping,
            gt_j_l,
            gt_j_r,
            gt_j_next,
            gt_c_l,
            gt_c_r,
            gt_c_next,
            gt_c_l_lmax,
            gt_c_r_lmax: Mapping::default(),
            j_buckets,
            c_buckets,
        }
    }

    fn gt_start_end(tidx: &SketchIndex, query_id: &str) -> (RPos, RPos, RPos, RPos, SegmId, String) {
        let parsed = ParsedQueryId::parse(query_id).expect("query_id must be ground-truth-encoded");
        let segm_id = tidx
            .segments
            .iter()
            .position(|s| s.name == parsed.segm_id)
            .expect("ground-truth segment not found in index") as SegmId;

        if AP {
            (
                parsed.start_pos,
                parsed.end_pos,
                parsed.start_pos,
                parsed.end_pos,
                segm_id,
                parsed.segm_id,
            )
        } else {
            let segm = tidx.get_segment(segm_id);
            let start = segm.kmers.partition_point(|k| k.r < parsed.start_pos) as RPos;
            let end = segm.kmers.partition_point(|k| k.r < parsed.end_pos) as RPos;
            (parsed.start_pos, parsed.end_pos, start, end, segm_id, parsed.segm_id)
        }
    }

    fn vec2str(&mut self, bucket_locs: &[BucketLoc]) -> String {
        let mut res = String::from("{");
        let mut max_j = 0.0_f64;
        let mut max_c = 0.0_f64;
        for b in bucket_locs {
            let m_slice = self.matcher.collect_matches(b, self.p_ht);
            let best_j = self
                .matcher
                .best_included_jaccard(&m_slice, self.p_sz, self.m - 2, self.m + 2, self.m);
            let best_c = self
                .matcher
                .best_fixed_length(&m_slice, self.p_sz, self.m, MappingMetric::ContainmentIndex);
            res.push_str(&format!(
                "({}, {}, {:.4}, {:.4}),",
                self.tidx.get_segment(b.segm_id).name,
                b.b,
                best_j.score(),
                best_c.score()
            ));
            max_j = max_j.max(best_j.score());
            max_c = max_c.max(best_c.score());
        }
        res.push('}');
        res.push_str(&format!("\tmax_J: {max_j:.4}\tmax_C: {max_c:.4}"));
        res
    }

    pub fn render_tsv_row(&mut self) -> String {
        let j_buckets = self.j_buckets.clone();
        let c_buckets = self.c_buckets.clone();
        let j_str = self.vec2str(&j_buckets);
        let c_str = self.vec2str(&c_buckets);
        format!(
            "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}",
            self.query_id,
            self.m,
            self.theta,
            self.bucket_l,
            self.segm_name,
            self.gt_b_l.b,
            self.gt_b_r.b,
            self.gt_b_next.b,
            self.gt_j_l.score(),
            self.gt_j_r.score(),
            self.gt_j_next.score(),
            self.gt_c_l.score(),
            self.gt_c_r.score(),
            self.gt_c_next.score(),
            self.gt_c_l_lmax.score(),
            self.gt_c_r_lmax.score(),
            self.j_buckets.len(),
            self.c_buckets.len(),
            j_str,
            c_str,
            self.p,
        )
    }

    pub fn print_paf(&self, out: &mut impl Write) -> std::io::Result<()> {
        write!(
            out,
            "\tgt_segm:s:{}\tgt_start_nucl:i:{}\tgt_end_nucl:i:{}\tbucket_l:i:{}\tgt_b_l:s:{}\tgt_b_r:s:{}\tgt_b_next:s:{}\tgt_M_l:i:{}\tgt_M_r:i:{}\tgt_M_next:i:{}\tgt_J_l:f:{:.5}\tgt_J_r:f:{:.5}\tgt_J_next:f:{:.5}\tgt_C_l:f:{:.5}\tgt_C_r:f:{:.5}\tgt_C_next:f:{:.5}\tgt_C_l_lmax:f:{:.5}\tgt_C_r_lmax:f:{:.5}",
            self.segm_name,
            self.gt_start_nucl,
            self.gt_end_nucl,
            self.bucket_l,
            self.gt_b_l,
            self.gt_b_r,
            self.gt_b_next,
            // gt_M_l/r/next sizes: re-collected on demand rather than
            // stored, since collect_matches is cheap and this avoids
            // holding three more borrowed Vec<Match> fields alive
            self.matcher.collect_matches(&self.gt_b_l, self.p_ht).len(),
            self.matcher.collect_matches(&self.gt_b_r, self.p_ht).len(),
            self.matcher.collect_matches(&self.gt_b_next, self.p_ht).len(),
            self.gt_j_l.score(),
            self.gt_j_r.score(),
            self.gt_j_next.score(),
            self.gt_c_l.score(),
            self.gt_c_r.score(),
            self.gt_c_next.score(),
            self.gt_c_l_lmax.score(),
            self.gt_c_r_lmax.score(),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::handler::Handler;
    use crate::params::Params;
    use crate::shmap::SHMapper;
    use clap::Parser;

    /// A small deterministic (xorshift-seeded) ACGT sequence — no `rand`
    /// dependency needed, just enough base diversity that a k=10 sketch
    /// of it is overwhelmingly likely to be unique, so a substring read
    /// maps cleanly back to one specific ground-truth region.
    fn pseudo_random_dna(seed: u64, len: usize) -> Vec<u8> {
        let bases = [b'A', b'C', b'G', b'T'];
        let mut state = seed;
        (0..len)
            .map(|_| {
                state ^= state << 13;
                state ^= state >> 7;
                state ^= state << 17;
                bases[(state % 4) as usize]
            })
            .collect()
    }

    #[test]
    fn ground_truth_bucket_reproduces_high_score_for_an_exact_substring_read() {
        let reference = pseudo_random_dna(12345, 200);
        let ref_fa = format!(">chr1\n{}\n", String::from_utf8(reference.clone()).unwrap());

        let read_start = 20usize;
        let read_end = 70usize;
        let read_seq = &reference[read_start..read_end];
        let query_id = format!("sim!chr1!{read_start}!{read_end}!+");
        let reads_fa = format!(">{query_id}\n{}\n", String::from_utf8(read_seq.to_vec()).unwrap());

        let mut ref_file = tempfile::Builder::new().suffix(".fa").tempfile().unwrap();
        ref_file.write_all(ref_fa.as_bytes()).unwrap();
        ref_file.flush().unwrap();
        let mut reads_file = tempfile::Builder::new().suffix(".fa").tempfile().unwrap();
        reads_file.write_all(reads_fa.as_bytes()).unwrap();
        reads_file.flush().unwrap();

        let params = Params::try_parse_from([
            "shmap",
            "-p",
            reads_file.path().to_str().unwrap(),
            "-s",
            ref_file.path().to_str().unwrap(),
            "-k",
            "10",
            "-r",
            "1.0",
            "-t",
            "0.5",
        ])
        .unwrap();
        let mut handler = Handler::new(params).unwrap();
        let mut tidx = SketchIndex::new();
        tidx.build_index(
            &handler.params.t_file.clone(),
            &handler.sketcher,
            handler.params.max_matches,
            &mut handler.counters,
            &mut handler.timers,
        )
        .unwrap();

        let mut mapper: SHMapper<false, false, false> = SHMapper::new(&tidx);
        let p = handler.sketcher.sketch(read_seq, &mut handler.counters);
        let m = p.len() as QPos;
        let mut p = p;
        let p_unique = mapper.unique_elements_with_info(&mut p);

        let mut p_ht: H2Seed = H2Seed::default();
        let mut diff_hist: H2Cnt = H2Cnt::default();
        for seed in &p_unique {
            p_ht.insert(seed.kmer.h, seed.clone());
            diff_hist.insert(seed.kmer.h, seed.occs_in_p);
        }

        let mut buckets: Buckets<false> = Buckets::new(&tidx);
        buckets.set_halflen(m);
        mapper.match_seeds(&p_unique, &mut buckets, p_unique.len() as QPos);
        buckets.propagate_seeds_to_buckets();

        let p_sz = read_seq.len() as QPos;
        let gt = AnalyseSimulatedReads::<false>::new(&query_id, read_seq, p_sz, diff_hist, m, &p_ht, &tidx, &mut buckets, 0.5);

        assert_eq!(gt.segm_id, 0);
        assert_eq!(gt.segm_name, "chr1");
        // The read is an exact substring of the reference at the claimed
        // ground-truth position, so its containment score there should be
        // high (not necessarily 1.0, since the bucket-boundary sweep can
        // clip a few k-mers at the edges).
        assert!(gt.gt_c_r.score() > 0.8, "gt_c_r score too low: {}", gt.gt_c_r.score());
    }
}
