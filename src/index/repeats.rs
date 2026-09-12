use anyhow::{Result, ensure};

use super::SketchIndex;
use crate::io::read_fasta;
use crate::sketch::{FracMinHash, RefSegment};
use crate::types::{RPos, SegmId};
use crate::utils::{Counters, Timers};

const BLOCK: usize = 8192;
const PADDING: usize = 32_768;

pub struct RepeatIndex {
    pub index: Box<SketchIndex>,
    pub sketcher: FracMinHash,
    regions: Vec<Vec<(usize, usize)>>,
}

impl RepeatIndex {
    pub fn covers(&self, segment: SegmId, start: i64, end: i64) -> bool {
        start >= 0
            && self.regions.get(segment as usize).is_some_and(|regions| {
                regions
                    .iter()
                    .any(|&(left, right)| left as i64 <= start && end <= right as i64)
            })
    }
}

impl SketchIndex {
    pub fn build_repeat_index(
        &mut self,
        reference: &str,
        k: i32,
        counters: &mut Counters,
        timers: &mut Timers,
    ) -> Result<()> {
        let sketcher = FracMinHash::new(k, 0.1);
        let mut regions = Vec::with_capacity(self.segments.len());
        for segment in &self.segments {
            let mut blocks = vec![(0usize, 0usize); (segment.sz as usize).div_ceil(BLOCK)];
            for kmer in &segment.kmers {
                let (total, repeated) = &mut blocks[kmer.r as usize / BLOCK];
                *total += 1;
                *repeated += usize::from(self.count(kmer.h) > 1);
            }
            let mut selected: Vec<(usize, usize)> = Vec::new();
            for (block, &(total, repeated)) in blocks.iter().enumerate() {
                if total < 8 || repeated * 4 < total {
                    continue;
                }
                let start = (block * BLOCK).saturating_sub(PADDING);
                let end = ((block + 1) * BLOCK + PADDING).min(segment.sz as usize);
                if let Some(last) = selected.last_mut()
                    && start <= last.1
                {
                    last.1 = last.1.max(end);
                } else {
                    selected.push((start, end));
                }
            }
            regions.push(selected);
        }
        let mut dense = SketchIndex::new();
        let mut segment_id = 0usize;
        let mut invalid = false;
        read_fasta(reference, timers, |name, sequence, _| {
            let Some(original) = self.segments.get(segment_id) else {
                invalid = true;
                return;
            };
            if name != original.name || sequence.len() != original.sz as usize {
                invalid = true;
                return;
            }
            let mut kmers = Vec::new();
            for &(start, end) in &regions[segment_id] {
                counters.inc("repeat_index_bases", (end - start) as i64);
                counters.inc1("repeat_index_regions");
                kmers.extend(sketcher.sketch_slice_into(&sequence[start..end], start as RPos, Vec::new()));
            }
            for (offset, kmer) in kmers.iter().enumerate() {
                let shard = &mut dense.shards[super::shard_of(kmer.h)];
                Self::insert_hit(shard, kmer, offset as RPos, segment_id as SegmId, None);
            }
            dense
                .segments
                .push(RefSegment::new(kmers, name.to_owned(), original.sz, original.id));
            segment_id += 1;
        })?;
        ensure!(
            !invalid && segment_id == self.segments.len(),
            "reference changed before dense indexing"
        );
        for shard in &mut dense.shards {
            Self::finalize_shard(shard);
        }
        dense.compact();
        self.repeats = Some(RepeatIndex {
            index: Box::new(dense),
            sketcher,
            regions,
        });
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::profiling::Profiler;

    #[test]
    fn regional_density_preserves_base_sketch_and_reference_coordinates() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("reference.fa");
        let sequence = "ACGTCGATGCTAGCTACGATCGATGCTAGCAT".repeat(2000);
        std::fs::write(&path, format!(">first\n{sequence}\n>second\n{sequence}\n")).unwrap();
        let mut index = SketchIndex::new();
        let mut counters = Counters::new();
        let mut timers = Timers::new();
        index
            .build_index(
                path.to_str().unwrap(),
                &FracMinHash::new(25, 0.05),
                None,
                &mut counters,
                &mut timers,
                &Profiler::new(false),
                1,
            )
            .unwrap();
        let before: Vec<_> = index
            .segments
            .iter()
            .map(|segment| segment.kmers.iter().map(|kmer| (kmer.h, kmer.r)).collect::<Vec<_>>())
            .collect();
        index
            .build_repeat_index(path.to_str().unwrap(), 25, &mut counters, &mut timers)
            .unwrap();
        for (original, segment) in before.iter().zip(&index.segments) {
            assert_eq!(
                *original,
                segment.kmers.iter().map(|kmer| (kmer.h, kmer.r)).collect::<Vec<_>>()
            );
        }
        let repeats = index.repeats.as_ref().unwrap();
        assert!(repeats.covers(0, 0, sequence.len() as i64));
        assert!(repeats.covers(1, 0, sequence.len() as i64));
        assert!(!repeats.covers(1, -1, 500));
        assert_eq!(repeats.index.segments.len(), 2);
    }
}
