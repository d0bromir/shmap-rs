use rustc_hash::FxHashMap;

use crate::index::SketchIndex;
use crate::sketch::FracMinHash;
use crate::types::{Kmer, SegmId};

const TILES: usize = 8;
const MAX_CANDIDATES: usize = 256;
const MAX_GLOBAL_OCCURRENCES: usize = 64;
const MAX_HITS: usize = 16_384;

#[derive(Clone, Copy)]
struct Sample {
    kmer: Kmer,
    tile: usize,
}

#[derive(Clone, Copy, Default)]
struct Vote {
    sum: i64,
    count: usize,
    tiles: u16,
}

#[derive(Clone, Copy, Debug)]
pub(super) struct Placement {
    pub segment: SegmId,
    pub reverse: bool,
    pub start: i64,
    pub matches: usize,
    pub sampled: usize,
}

impl Placement {
    pub fn paf(&self, index: &SketchIndex, query: &str, length: usize, k: i32) -> String {
        let segment = index.get_segment(self.segment);
        let survival = self.matches as f64 / self.sampled as f64;
        let estimated_matches = (length as f64 * survival.powf(1.0 / k as f64)).round() as usize;
        format!(
            "{query}\t{length}\t0\t{length}\t{}\t{}\t{}\t{}\t{}\t{}\t{length}\t255\tam:Z:adaptive-v1\tss:f:{survival:.6}\tna:i:{}\tns:i:{}\n",
            if self.reverse { '-' } else { '+' },
            segment.name,
            segment.sz,
            self.start,
            self.start + length as i64,
            estimated_matches.min(length),
            self.matches,
            self.sampled,
        )
    }
}

#[derive(Default)]
pub(super) struct Adaptive {
    sample: Vec<Sample>,
    buffer: Vec<Kmer>,
    votes: FxHashMap<(SegmId, bool, i64), Vote>,
    candidates: Vec<Placement>,
    anchors: Vec<(i64, i64, usize)>,
    pub bases: usize,
    pub hits: usize,
    pub checked: usize,
}

impl Adaptive {
    pub fn locate(
        &mut self,
        index: &SketchIndex,
        sketcher: &FracMinHash,
        sequence: &[u8],
        threshold: f64,
    ) -> Option<Placement> {
        self.bases = 0;
        self.hits = 0;
        self.checked = 0;
        if sequence.len() < 4096 || sequence.len() > i32::MAX as usize || sketcher.k > 64 {
            return None;
        }
        let tolerance = (sequence.len() as i64 / 100).max(64);
        for width in [128, 256, 512] {
            self.sample.clear();
            for tile in 0..TILES {
                let start = tile * (sequence.len() - width) / (TILES - 1);
                let region = &sequence[start..start + width];
                if !region
                    .iter()
                    .all(|base| matches!(base, b'A' | b'C' | b'G' | b'T' | b'a' | b'c' | b'g' | b't'))
                {
                    return None;
                }
                self.buffer = sketcher.sketch_slice_into(region, start as i32, std::mem::take(&mut self.buffer));
                self.bases += region.len();
                self.sample
                    .extend(self.buffer.iter().map(|&kmer| Sample { kmer, tile }));
            }
            if self.sample.len() < 10 {
                continue;
            }
            self.votes.clear();
            for sample in &self.sample {
                let hits = index.hits(sample.kmer.h);
                if hits.len() > MAX_GLOBAL_OCCURRENCES {
                    continue;
                }
                for hit in hits {
                    self.hits += 1;
                    if self.hits > MAX_HITS {
                        return None;
                    }
                    let reverse = hit.strand != sample.kmer.strand;
                    let query_position = oriented_position(sample.kmer.r, reverse, sequence.len(), sketcher.k);
                    let origin = hit.r as i64 - query_position;
                    let bin = origin.div_euclid(tolerance);
                    for offset in [0, -1] {
                        let vote = self.votes.entry((hit.segm_id, reverse, bin + offset)).or_default();
                        vote.sum += origin;
                        vote.count += 1;
                        vote.tiles |= 1 << sample.tile;
                    }
                    if self.votes.len() > MAX_CANDIDATES {
                        return None;
                    }
                }
            }
            self.candidates.clear();
            for (&(segment, reverse, _), vote) in &self.votes {
                if vote.count >= 6 && vote.tiles.count_ones() >= 3 {
                    self.candidates.push(Placement {
                        segment,
                        reverse,
                        start: vote.sum / vote.count as i64,
                        matches: 0,
                        sampled: self.sample.len(),
                    });
                }
            }
            self.candidates
                .sort_unstable_by_key(|candidate| (candidate.segment, candidate.reverse, candidate.start));
            self.candidates.dedup_by(|right, left| {
                right.segment == left.segment
                    && right.reverse == left.reverse
                    && (right.start - left.start).abs() <= tolerance
            });
            let mut best: Option<Placement> = None;
            let mut second = 0;
            for candidate_index in 0..self.candidates.len() {
                let mut candidate = self.candidates[candidate_index];
                self.checked += 1;
                self.anchors.clear();
                for sample in &self.sample {
                    let hits = index.hits(sample.kmer.h);
                    let query_position =
                        oriented_position(sample.kmer.r, candidate.reverse, sequence.len(), sketcher.k);
                    let expected = candidate.start + query_position;
                    let begin = hits
                        .partition_point(|hit| (hit.segm_id, hit.r as i64) < (candidate.segment, expected - tolerance));
                    let mut nearest: Option<i64> = None;
                    for hit in &hits[begin..] {
                        if hit.segm_id != candidate.segment || hit.r as i64 > expected + tolerance {
                            break;
                        }
                        self.hits += 1;
                        if self.hits > MAX_HITS {
                            return None;
                        }
                        if (hit.strand != sample.kmer.strand) == candidate.reverse
                            && nearest
                                .is_none_or(|previous| (hit.r as i64 - expected).abs() < (previous - expected).abs())
                        {
                            nearest = Some(hit.r as i64);
                        }
                    }
                    if let Some(position) = nearest {
                        self.anchors.push((query_position, position, sample.tile));
                    }
                }
                self.anchors.sort_unstable();
                let mut tiles = 0u16;
                let mut previous = (-1i64, -1i64);
                let mut origin_sum = 0;
                for &(query_position, position, tile) in &self.anchors {
                    if query_position > previous.0 && position > previous.1 {
                        candidate.matches += 1;
                        tiles |= 1 << tile;
                        origin_sum += position - query_position;
                        previous = (query_position, position);
                    }
                }
                if candidate.matches == 0 {
                    continue;
                }
                candidate.start = origin_sum / candidate.matches as i64;
                let covers_read = tiles.count_ones() >= 6 && tiles & 1 != 0 && tiles & (1 << (TILES - 1)) != 0;
                let in_reference = candidate.start >= 0
                    && candidate.start + sequence.len() as i64 <= index.get_segment(candidate.segment).sz as i64;
                if covers_read && in_reference && best.is_none_or(|current| candidate.matches > current.matches) {
                    if let Some(current) = best {
                        second = second.max(current.matches);
                    }
                    best = Some(candidate);
                } else {
                    second = second.max(candidate.matches);
                }
            }
            if let Some(best) = best
                && best.matches >= 10
                && best.matches as f64 >= self.sample.len() as f64 * threshold.max(0.65)
                && (second as f64) < best.matches as f64 * 0.8
            {
                return Some(best);
            }
        }
        None
    }
}

fn oriented_position(position: i32, reverse: bool, length: usize, k: i32) -> i64 {
    if reverse {
        length as i64 + k as i64 - 2 - position as i64
    } else {
        position as i64
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sketch::RefSegment;
    use crate::types::Hit;

    fn fixture(duplicate: bool) -> (SketchIndex, FracMinHash, Vec<u8>) {
        let mut state = 42u64;
        let sequence: Vec<_> = (0..30_000)
            .map(|_| {
                state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
                b"ACGT"[(state >> 32) as usize & 3]
            })
            .collect();
        let sketcher = FracMinHash::new(25, 0.05);
        let mut index = SketchIndex::new();
        for segment in 0..if duplicate { 2 } else { 1 } {
            let kmers = sketcher.sketch_into(&sequence, Vec::new());
            for (offset, kmer) in kmers.iter().enumerate() {
                let hit = Hit::new(kmer, offset as i32, segment);
                let shard = &mut index.shards[kmer.h as usize & 7];
                if let Some(previous) = shard.h2single.remove(&kmer.h) {
                    shard.h2multi.insert(kmer.h, vec![previous, hit]);
                } else if let Some(hits) = shard.h2multi.get_mut(&kmer.h) {
                    hits.push(hit);
                } else {
                    shard.h2single.insert(kmer.h, hit);
                }
            }
            index.segments.push(RefSegment::new(
                kmers,
                format!("ref{segment}"),
                sequence.len() as i32,
                segment,
            ));
        }
        (index, sketcher, sequence)
    }

    #[test]
    fn adaptive_maps_both_orientations_and_rescues_ambiguous_reads() {
        let (index, sketcher, sequence) = fixture(false);
        let read = &sequence[5000..17_000];
        let mut mapper = Adaptive::default();
        let forward = mapper.locate(&index, &sketcher, read, 0.4).unwrap();
        assert_eq!((forward.segment, forward.start, forward.reverse), (0, 5000, false));
        assert!(mapper.bases < read.len());
        let reverse: Vec<_> = read
            .iter()
            .rev()
            .map(|base| match base {
                b'A' => b'T',
                b'C' => b'G',
                b'G' => b'C',
                _ => b'A',
            })
            .collect();
        let reverse = mapper.locate(&index, &sketcher, &reverse, 0.4).unwrap();
        assert_eq!((reverse.segment, reverse.start, reverse.reverse), (0, 5000, true));
        assert!(mapper.locate(&index, &sketcher, &[b'N'; 5000], 0.4).is_none());
        assert!(mapper.locate(&index, &sketcher, &read[..100], 0.4).is_none());
        let (repeated, sketcher, sequence) = fixture(true);
        assert!(
            mapper
                .locate(&repeated, &sketcher, &sequence[5000..17_000], 0.4)
                .is_none()
        );
    }
}
