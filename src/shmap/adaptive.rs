use rustc_hash::{FxHashMap, FxHashSet};

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
    pub dense: bool,
}

impl Placement {
    pub fn paf(&self, index: &SketchIndex, query: &str, length: usize, k: i32) -> String {
        let segment = index.get_segment(self.segment);
        let survival = self.matches as f64 / self.sampled as f64;
        let estimated_matches = (length as f64 * survival.powf(1.0 / k as f64)).round() as usize;
        let method = if self.dense { "adaptive-dense-v1" } else { "adaptive-v1" };
        format!(
            "{query}\t{length}\t0\t{length}\t{}\t{}\t{}\t{}\t{}\t{}\t{length}\t255\tam:Z:{method}\tss:f:{survival:.6}\tna:i:{}\tns:i:{}\n",
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
    sample_heads: FxHashMap<u64, usize>,
    sample_next: Vec<usize>,
    nearest: Vec<Option<i64>>,
    pub bases: usize,
    pub hits: usize,
    pub checked: usize,
    rescue_ready: bool,
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
        self.rescue_ready = false;
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
                        dense: false,
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
        self.rescue_ready = true;
        None
    }

    pub fn resolve_repeats(&mut self, index: &SketchIndex, sequence: &[u8], threshold: f64) -> Option<Placement> {
        let repeats = index.repeats.as_ref()?;
        if !self.rescue_ready || !(2..=16).contains(&self.candidates.len()) {
            return None;
        }
        let tolerance = (sequence.len() as i64 / 100).max(64);
        if !self.candidates.iter().all(|candidate| {
            repeats.covers(
                candidate.segment,
                candidate.start,
                candidate.start + sequence.len() as i64,
            )
        }) {
            return None;
        }
        self.sample.clear();
        let width = (sequence.len() / TILES).min(1024);
        for tile in 0..TILES {
            let start = tile * (sequence.len() - width) / (TILES - 1);
            let region = &sequence[start..start + width];
            if !region
                .iter()
                .all(|base| matches!(base, b'A' | b'C' | b'G' | b'T' | b'a' | b'c' | b'g' | b't'))
            {
                return None;
            }
            self.buffer = repeats
                .sketcher
                .sketch_slice_into(region, start as i32, std::mem::take(&mut self.buffer));
            self.bases += region.len();
            self.sample
                .extend(self.buffer.iter().map(|&kmer| Sample { kmer, tile }));
        }
        let mut scored = Vec::new();
        let mut work = 0;
        static POSTING_LOOKUPS: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
        let posting_lookups =
            *POSTING_LOOKUPS.get_or_init(|| std::env::var_os("SHMAP_DENSE_POSTING_LOOKUPS").is_some());
        if !posting_lookups {
            self.sample_heads.clear();
            self.sample_next.clear();
            for (sample_index, sample) in self.sample.iter().enumerate() {
                let previous = self.sample_heads.insert(sample.kmer.h, sample_index);
                self.sample_next.push(previous.unwrap_or(usize::MAX));
            }
        }
        for candidate_index in 0..self.candidates.len() {
            let mut placement = self.candidates[candidate_index];
            if !posting_lookups {
                self.scan_candidate(
                    &repeats.index,
                    &placement,
                    sequence.len(),
                    repeats.sketcher.k,
                    tolerance,
                    &mut work,
                )?;
            }
            placement.matches = 0;
            placement.sampled = self.sample.len();
            placement.dense = true;
            self.checked += 1;
            let mut support = FxHashSet::default();
            let mut tiles = 0u16;
            let mut origin_sum = 0i64;
            let mut previous = (-1i64, -1i64);
            for ordinal in 0..self.sample.len() {
                let sample_index = if placement.reverse {
                    self.sample.len() - 1 - ordinal
                } else {
                    ordinal
                };
                let sample = &self.sample[sample_index];
                let query_position =
                    oriented_position(sample.kmer.r, placement.reverse, sequence.len(), repeats.sketcher.k);
                let expected = placement.start + query_position;
                let mut nearest = if posting_lookups {
                    None
                } else {
                    self.nearest[sample_index]
                };
                if posting_lookups {
                    let hits = repeats.index.hits(sample.kmer.h);
                    let begin = hits
                        .partition_point(|hit| (hit.segm_id, hit.r as i64) < (placement.segment, expected - tolerance));
                    for hit in &hits[begin..] {
                        if hit.segm_id != placement.segment || hit.r as i64 > expected + tolerance {
                            break;
                        }
                        self.hits += 1;
                        work += 1;
                        if work > MAX_HITS * 4 {
                            return None;
                        }
                        if (hit.strand != sample.kmer.strand) == placement.reverse
                            && nearest
                                .is_none_or(|position| (hit.r as i64 - expected).abs() < (position - expected).abs())
                        {
                            nearest = Some(hit.r as i64);
                        }
                    }
                }
                if let Some(position) = nearest
                    && query_position > previous.0
                    && position > previous.1
                {
                    placement.matches += 1;
                    support.insert(sample.kmer.h);
                    tiles |= 1 << sample.tile;
                    origin_sum += position - query_position;
                    previous = (query_position, position);
                }
            }
            if placement.matches > 0 {
                placement.start = origin_sum / placement.matches as i64;
            }
            scored.push((placement, support, tiles));
        }
        scored.sort_by(|left, right| {
            right.0.matches.cmp(&left.0.matches).then_with(|| {
                (left.0.segment, left.0.reverse, left.0.start).cmp(&(right.0.segment, right.0.reverse, right.0.start))
            })
        });
        let (best, support, tiles) = &scored[0];
        if best.matches < 20
            || (best.matches as f64) < best.sampled as f64 * threshold.max(0.65)
            || tiles.count_ones() < 6
            || tiles & 1 == 0
            || tiles & (1 << (TILES - 1)) == 0
            || !repeats.covers(best.segment, best.start, best.start + sequence.len() as i64)
        {
            return None;
        }
        for (rival, rival_support, _) in &scored[1..] {
            if best.matches <= rival.matches {
                return None;
            }
            let mut independent = 0;
            let mut evidence_tiles = 0u16;
            let mut previous = -(repeats.sketcher.k as i64) * 2;
            for sample in &self.sample {
                if support.contains(&sample.kmer.h)
                    && !rival_support.contains(&sample.kmer.h)
                    && sample.kmer.r as i64 - previous >= repeats.sketcher.k as i64 * 2
                {
                    independent += 1;
                    evidence_tiles |= 1 << sample.tile;
                    previous = sample.kmer.r as i64;
                }
            }
            if independent < 3 || evidence_tiles.count_ones() < 2 {
                return None;
            }
        }
        Some(*best)
    }

    fn scan_candidate(
        &mut self,
        index: &SketchIndex,
        placement: &Placement,
        length: usize,
        k: i32,
        tolerance: i64,
        work: &mut usize,
    ) -> Option<()> {
        self.nearest.clear();
        self.nearest.resize(self.sample.len(), None);
        let sketch = &index.get_segment(placement.segment).kmers;
        let start = placement.start - tolerance;
        let end = placement.start + length as i64 + k as i64 + tolerance;
        let begin = sketch.partition_point(|kmer| (kmer.r as i64) < start);
        for kmer in &sketch[begin..] {
            if kmer.r as i64 > end {
                break;
            }
            *work += 1;
            if *work > MAX_HITS * 4 {
                return None;
            }
            let Some(&head) = self.sample_heads.get(&kmer.h) else {
                continue;
            };
            let mut sample_index = head;
            while sample_index != usize::MAX {
                *work += 1;
                if *work > MAX_HITS * 4 {
                    return None;
                }
                let sample = &self.sample[sample_index];
                let expected = placement.start + oriented_position(sample.kmer.r, placement.reverse, length, k);
                if (kmer.r as i64 - expected).abs() <= tolerance {
                    self.hits += 1;
                    let nearest = &mut self.nearest[sample_index];
                    if (kmer.strand != sample.kmer.strand) == placement.reverse
                        && nearest.is_none_or(|previous| (kmer.r as i64 - expected).abs() < (previous - expected).abs())
                    {
                        *nearest = Some(kmer.r as i64);
                    }
                }
                sample_index = self.sample_next[sample_index];
            }
        }
        Some(())
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

    #[test]
    fn candidate_scan_matches_posting_search_with_duplicates_and_reverse_strands() {
        let (index, sketcher, _) = fixture(true);
        let mut mapper = Adaptive::default();
        for kmer in index.segments[0]
            .kmers
            .iter()
            .filter(|kmer| (5000..17_000).contains(&kmer.r))
        {
            let mut query = *kmer;
            query.r -= 5000;
            mapper.sample.push(Sample { kmer: query, tile: 0 });
        }
        mapper.sample.push(mapper.sample[0]);
        for reverse in [false, true] {
            if reverse {
                for sample in &mut mapper.sample {
                    sample.kmer.r = oriented_position(sample.kmer.r, true, 12_000, sketcher.k) as i32;
                    sample.kmer.strand = !sample.kmer.strand;
                }
            }
            mapper.sample_heads.clear();
            mapper.sample_next.clear();
            for (sample_index, sample) in mapper.sample.iter().enumerate() {
                let previous = mapper.sample_heads.insert(sample.kmer.h, sample_index);
                mapper.sample_next.push(previous.unwrap_or(usize::MAX));
            }
            let placement = Placement {
                segment: 1,
                reverse,
                start: 5000,
                matches: 0,
                sampled: mapper.sample.len(),
                dense: true,
            };
            mapper
                .scan_candidate(&index, &placement, 12_000, sketcher.k, 120, &mut 0)
                .unwrap();
            for (sample, nearest) in mapper.sample.iter().zip(&mapper.nearest) {
                let expected = 5000 + oriented_position(sample.kmer.r, reverse, 12_000, sketcher.k);
                let oracle = index
                    .hits(sample.kmer.h)
                    .iter()
                    .filter(|hit| {
                        hit.segm_id == 1
                            && (hit.strand != sample.kmer.strand) == reverse
                            && (hit.r as i64 - expected).abs() <= 120
                    })
                    .min_by_key(|hit| (hit.r as i64 - expected).abs())
                    .map(|hit| hit.r as i64);
                assert_eq!(*nearest, oracle);
            }
            assert!(
                mapper
                    .scan_candidate(&index, &placement, 12_000, sketcher.k, 120, &mut (MAX_HITS * 4))
                    .is_none()
            );
        }
    }

    #[test]
    fn dense_rescue_distinguishes_variants_but_not_identical_copies() {
        use crate::{
            profiling::Profiler,
            utils::{Counters, Timers},
        };
        let (_, _, sequence) = fixture(false);
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("repeat.fa");
        for identical in [false, true] {
            let mut copy = sequence.clone();
            if !identical {
                for position in (100..copy.len()).step_by(401) {
                    copy[position] = if copy[position] == b'A' { b'C' } else { b'A' };
                }
            }
            std::fs::write(
                &path,
                format!(
                    ">first\n{}\n>second\n{}\n",
                    String::from_utf8_lossy(&sequence),
                    String::from_utf8_lossy(&copy)
                ),
            )
            .unwrap();
            let sketcher = FracMinHash::new(25, 0.01);
            let mut index = SketchIndex::new();
            let mut counters = Counters::new();
            let mut timers = Timers::new();
            index
                .build_index(
                    path.to_str().unwrap(),
                    &sketcher,
                    None,
                    &mut counters,
                    &mut timers,
                    &Profiler::new(false),
                    1,
                )
                .unwrap();
            index
                .build_repeat_index(path.to_str().unwrap(), 25, &mut counters, &mut timers)
                .unwrap();
            let mut mapper = Adaptive::default();
            let read = &sequence[5000..17_000];
            assert!(mapper.locate(&index, &sketcher, read, 0.4).is_none());
            let result = mapper.resolve_repeats(&index, read, 0.4);
            if identical {
                assert!(result.is_none());
            } else {
                let placement = result.unwrap();
                assert_eq!((placement.segment, placement.start, placement.dense), (0, 5000, true));
            }
        }
    }
}
