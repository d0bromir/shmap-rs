//! Reference k-mer index.
//!
//! Port of `shmap/src/index.h`.

use rustc_hash::FxHashMap;

use crate::io::read_fasta;
use crate::sketch::{FracMinHash, RefSegment, SketchT};
use crate::types::{Hash, Hit, RPos, SegmId};
use crate::utils::{Counters, ProgressBar, Timers};

/// An indexed reference: k-mer sketches of every segment, plus a hash map
/// from k-mer hash to its hit(s) in the reference.
///
/// The C++ `SketchIndex` also stores a `Handler *H` back-pointer purely so
/// its methods can bump shared counters/timers; this port takes those as
/// explicit parameters instead (the same convention the C++ itself already
/// uses for e.g. `SHMapper::map_read`'s `params`/`sketcher` arguments),
/// which keeps `SketchIndex` plain, aliasing-free data.
#[derive(Default)]
pub struct SketchIndex {
    pub segments: Vec<RefSegment>,
    /// K-mers with exactly one hit in the reference.
    pub h2single: FxHashMap<Hash, Hit>,
    /// K-mers with more than one hit, each list sorted by `(segm_id, r)`
    /// (equivalently `(segm_id, tpos)`) to allow binary search.
    pub h2multi: FxHashMap<Hash, Vec<Hit>>,
}

impl SketchIndex {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn segments_len(&self) -> usize {
        self.segments.len()
    }

    pub fn get_segment_by_name(&self, name: &str) -> Option<&RefSegment> {
        self.segments.iter().find(|s| s.name == name)
    }

    pub fn get_segment(&self, segm_id: SegmId) -> &RefSegment {
        &self.segments[segm_id as usize]
    }

    /// Number of hits in the reference for k-mer hash `h`.
    pub fn count(&self, h: Hash) -> RPos {
        if self.h2single.contains_key(&h) {
            return 1;
        }
        if let Some(hits) = self.h2multi.get(&h) {
            return hits.len() as RPos;
        }
        0
    }

    fn populate_h2pos(&mut self, sketch: &SketchT, segm_id: SegmId, max_matches: Option<i32>) {
        for (tpos, kmer) in sketch.iter().enumerate() {
            let hit = Hit::new(kmer, tpos as RPos, segm_id);
            if !self.h2single.contains_key(&kmer.h) {
                self.h2single.insert(kmer.h, hit);
            } else {
                let multi = self.h2multi.entry(kmer.h).or_default();
                if max_matches.is_none_or(|m| (multi.len() as i32) < m + 1) {
                    multi.push(hit);
                }
            }
        }
    }

    fn add_segment(
        &mut self,
        segm_name: String,
        segm_sz: RPos,
        sketch: SketchT,
        max_matches: Option<i32>,
        counters: &mut Counters,
    ) {
        let segm_id = self.segments.len() as SegmId;
        counters.inc1("segments");
        counters.inc("total_nucls", segm_sz as i64);
        // Populate from `sketch` (a local, not-yet-stored value) before
        // moving it into `self.segments`, so this doesn't need to borrow
        // `self.segments` immutably while `populate_h2pos` borrows `self`
        // mutably.
        self.populate_h2pos(&sketch, segm_id, max_matches);
        self.segments
            .push(RefSegment::new(sketch, segm_name, segm_sz, segm_id));
    }

    fn get_kmer_stats(&self, counters: &mut Counters) {
        let mut max_occ: RPos = 0;
        counters.inc("indexed_hits", self.h2single.len() as i64);
        counters.inc("indexed_kmers", self.h2single.len() as i64);
        for hits in self.h2multi.values() {
            let occ = hits.len() as RPos;
            counters.inc("indexed_hits", occ as i64);
            counters.inc1("indexed_kmers");
            if occ > max_occ {
                max_occ = occ;
            }
        }
        counters.inc("indexed_highest_freq_kmer", max_occ as i64);
    }

    fn erase_frequent_kmers(&mut self, max_matches: i32, counters: &mut Counters) {
        let blacklisted: Vec<Hash> = self
            .h2multi
            .iter()
            .filter(|(_, hits)| hits.len() as i32 > max_matches)
            .map(|(h, hits)| {
                counters.inc1("blacklisted_kmers");
                counters.inc("blacklisted_hits", hits.len() as i64);
                *h
            })
            .collect();
        for h in blacklisted {
            self.h2multi.remove(&h);
        }
    }

    /// Reads `t_file`, sketches each segment, and populates the index.
    pub fn build_index(
        &mut self,
        t_file: &str,
        sketcher: &FracMinHash,
        max_matches: Option<i32>,
        counters: &mut Counters,
        timers: &mut Timers,
    ) -> anyhow::Result<()> {
        let progress_bar = ProgressBar::new("Indexing");

        // Pre-register so `print_stats` can report 0 rather than panic if
        // the reference file turns out to have zero segments.
        counters.init(&["segments", "total_nucls"]);

        timers.start("indexing");
        eprintln!("Indexing {t_file}...");
        timers.start("index_reading");

        read_fasta(t_file, |segm_name, seq, indexing_progress| {
            timers.stop("index_reading");
            timers.start("index_sketching");
            let sketch = sketcher.sketch(seq, counters);
            timers.stop("index_sketching");

            timers.start("index_initializing");
            self.add_segment(
                segm_name.to_string(),
                seq.len() as RPos,
                sketch,
                max_matches,
                counters,
            );
            timers.stop("index_initializing");

            progress_bar.update(indexing_progress as f64);

            timers.start("index_reading");
        })?;
        eprintln!();

        // Migrate any k-mer that ended up in both `h2single` and `h2multi`
        // (its second occurrence was discovered after the first was
        // already placed in `h2single`) fully into `h2multi`, then sort
        // each multi-hit list by `(segm_id, r)` to allow binary search.
        for (h, hits) in self.h2multi.iter_mut() {
            if let Some(single_hit) = self.h2single.remove(h) {
                hits.push(single_hit);
            }
            hits.sort_by(|a, b| a.segm_id.cmp(&b.segm_id).then(a.r.cmp(&b.r)));
        }
        timers.stop("index_reading");
        timers.stop("indexing");

        self.get_kmer_stats(counters);
        counters.inc("blacklisted_kmers", 0);
        counters.inc("blacklisted_hits", 0);
        if let Some(max_matches) = max_matches {
            self.erase_frequent_kmers(max_matches, counters);
        }
        self.print_stats(sketcher.k, counters, timers);
        Ok(())
    }

    fn print_stats(&self, k: i32, counters: &Counters, timers: &Timers) {
        eprintln!(" | total nucleotides:     {}", counters.count("total_nucls"));
        eprintln!(
            " | index segments:        {} (~{:.1} per segment)",
            counters.count("segments"),
            counters.count("total_nucls") as f64 / counters.count("segments") as f64
        );
        for segm in &self.segments {
            eprintln!(" | | {} ({} nb)", segm.name, segm.sz);
        }
        eprintln!(" | indexed kmers:         {}", counters.count("indexed_kmers"));
        eprintln!(
            " | indexed hits:          {} ({:.1}% of the index, ~{:.1} per kmer)",
            counters.count("indexed_hits"),
            k as f64 * counters.perc("indexed_hits", "total_nucls"),
            counters.frac("indexed_hits", "indexed_kmers")
        );
        eprintln!(
            " | | most frequent kmer:      {} times.",
            counters.count("indexed_highest_freq_kmer")
        );
        eprintln!(
            " | | blacklisted kmers:       {} ({:.1}%)",
            counters.count("blacklisted_kmers"),
            counters.perc("blacklisted_kmers", "indexed_kmers")
        );
        eprintln!(
            " | | blacklisted hits:        {} ({:.1}%)",
            counters.count("blacklisted_hits"),
            counters.perc("blacklisted_hits", "indexed_hits")
        );
        eprintln!(" | indexing time:        {:.1}s", timers.secs("indexing"));
        eprintln!(" | | reading time:          {:.1}", timers.secs("index_reading"));
        eprintln!(" | | sketching time:        {:.1}", timers.secs("index_sketching"));
        eprintln!(" | | initializing time:     {:.1}", timers.secs("index_initializing"));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn fasta_file(content: &str) -> tempfile::NamedTempFile {
        let mut f = tempfile::Builder::new().suffix(".fa").tempfile().unwrap();
        write!(f, "{content}").unwrap();
        f.flush().unwrap();
        f
    }

    #[test]
    fn single_segment_indexing_matches_expected_hit_counts() {
        let f = fasta_file(">ref\nACCAGTACCA\n");
        let sketcher = FracMinHash::new(4, 1.0);
        let mut counters = Counters::new();
        let mut timers = Timers::new();
        let mut tidx = SketchIndex::new();
        tidx.build_index(f.path().to_str().unwrap(), &sketcher, None, &mut counters, &mut timers)
            .unwrap();

        let t = sketcher.sketch(b"ACCAGTACCA", &mut Counters::new());
        assert_eq!(t.len(), 7);
        let expected = [2, 1, 1, 1, 1, 1, 2];
        for (kmer, &want) in t.iter().zip(expected.iter()) {
            assert_eq!(tidx.count(kmer.h), want);
        }
        assert_eq!(counters.count("indexed_hits"), 7);
        assert_eq!(counters.count("indexed_kmers"), 6);
    }

    #[test]
    fn two_segments_share_kmer_counts_across_both() {
        let f = fasta_file(">segm1\nACCAGTACCA\n>segm2\nGGACCA\n");
        let sketcher = FracMinHash::new(4, 1.0);
        let mut counters = Counters::new();
        let mut timers = Timers::new();
        let mut tidx = SketchIndex::new();
        tidx.build_index(f.path().to_str().unwrap(), &sketcher, None, &mut counters, &mut timers)
            .unwrap();

        let t1 = sketcher.sketch(b"ACCAGTACCA", &mut Counters::new());
        assert_eq!(t1.len(), 7);
        let expected = [3, 1, 1, 1, 1, 1, 3];
        for (kmer, &want) in t1.iter().zip(expected.iter()) {
            assert_eq!(tidx.count(kmer.h), want);
        }
        assert_eq!(counters.count("indexed_hits"), 10);
        assert_eq!(counters.count("indexed_kmers"), 8);
    }
}
