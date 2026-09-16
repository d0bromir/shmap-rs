use anyhow::{Result, ensure};
use clap::{Parser, ValueEnum};
use rustc_hash::FxHashMap;
use shmap::index::SketchIndex;
use shmap::mapping::Mapping;
use shmap::shmap::SHMapper;
use shmap::sketch::{FracMinHash, RefSegment};
use shmap::types::{H2Seed, Hash, Kmer, Metric, QPos, Seed};
use std::path::PathBuf;
use std::time::Instant;

#[derive(Parser)]
struct Args {
    #[arg(long)]
    reference: PathBuf,
    #[arg(long)]
    reads: PathBuf,
    #[arg(long, default_value_t = 64)]
    block_size: usize,
    #[arg(long, value_enum, default_value_t = Partition::Fixed)]
    partition: Partition,
    #[arg(long, value_enum, default_value_t = Bounds::Sparse)]
    bounds: Bounds,
    #[arg(long, default_value_t = 32)]
    limit: usize,
    #[arg(long, default_value_t = 25)]
    k: i32,
    #[arg(long, default_value_t = 0.01)]
    density: f64,
    #[arg(long, default_value_t = 0.4)]
    threshold: f64,
}

#[derive(Clone, Copy, Debug, ValueEnum)]
enum Partition {
    Fixed,
    Content,
}

#[derive(Clone, Copy, Debug, ValueEnum)]
enum Bounds {
    Dense,
    Sparse,
}

enum Prefix {
    Dense(Vec<u64>),
    Sparse(Vec<(usize, u64)>),
}

impl Prefix {
    fn sum(&self, first: usize, last: usize) -> u64 {
        match self {
            Self::Dense(prefix) => prefix[last] - prefix[first],
            Self::Sparse(prefix) => {
                let before = |position| {
                    let count = prefix.partition_point(|&(block, _)| block < position);
                    if count == 0 { 0 } else { prefix[count - 1].1 }
                };
                before(last) - before(first)
            }
        }
    }
}

struct Layout {
    blocks: Vec<usize>,
    ends: Vec<usize>,
}

struct FactoredIndex {
    layouts: Vec<Layout>,
    postings: FxHashMap<Hash, Vec<(usize, usize)>>,
    occurrences: Vec<Vec<(usize, usize)>>,
    unique_blocks: usize,
    blocks: usize,
    tokens: usize,
    unique_tokens: usize,
}

impl FactoredIndex {
    fn build(segments: &[RefSegment], block_size: usize, partition: Partition) -> Self {
        assert!(block_size > 0);
        let mut dictionary: FxHashMap<Vec<(Hash, bool)>, usize> = FxHashMap::default();
        let mut postings: FxHashMap<Hash, Vec<(usize, usize)>> = FxHashMap::default();
        let mut layouts = Vec::new();
        let mut occurrences: Vec<Vec<(usize, usize)>> = Vec::new();
        let mut tokens = 0;
        let mut unique_tokens = 0;
        let mut blocks = 0;
        for segment in segments {
            let mut layout = Layout {
                blocks: Vec::new(),
                ends: Vec::new(),
            };
            let mut begin = 0;
            for end in 1..=segment.kmers.len() {
                let boundary = match partition {
                    Partition::Fixed => end - begin == block_size,
                    Partition::Content => {
                        segment.kmers[end - 1].h % block_size as u64 == 0 || end - begin == block_size.saturating_mul(4)
                    }
                };
                if !boundary && end != segment.kmers.len() {
                    continue;
                }
                let block = &segment.kmers[begin..end];
                let key: Vec<_> = block.iter().map(|kmer| (kmer.h, kmer.strand)).collect();
                let next = dictionary.len();
                let block_id = *dictionary.entry(key).or_insert_with(|| {
                    let mut counts = FxHashMap::default();
                    for kmer in block {
                        *counts.entry(kmer.h).or_insert(0usize) += 1;
                    }
                    for (hash, count) in counts {
                        postings.entry(hash).or_default().push((next, count));
                    }
                    unique_tokens += block.len();
                    next
                });
                if block_id == occurrences.len() {
                    occurrences.push(Vec::new());
                }
                occurrences[block_id].push((layouts.len(), layout.blocks.len()));
                layout.blocks.push(block_id);
                layout.ends.push(end);
                tokens += block.len();
                blocks += 1;
                begin = end;
            }
            layouts.push(layout);
        }
        Self {
            layouts,
            postings,
            occurrences,
            unique_blocks: dictionary.len(),
            blocks,
            tokens,
            unique_tokens,
        }
    }

    fn payload_bytes(&self) -> usize {
        self.blocks * 2 * size_of::<usize>()
            + self.blocks * size_of::<(usize, usize)>()
            + self.postings.len() * size_of::<Hash>()
            + self
                .postings
                .values()
                .map(|hits| hits.len() * size_of::<(usize, usize)>())
                .sum::<usize>()
    }

    fn query_prefixes(&self, query: &H2Seed, bounds: Bounds) -> (Vec<Prefix>, usize) {
        if matches!(bounds, Bounds::Sparse) {
            let mut masses: FxHashMap<usize, u64> = FxHashMap::default();
            let mut visits = 0;
            for seed in query.values() {
                if let Some(postings) = self.postings.get(&seed.kmer.h) {
                    visits += postings.len();
                    for &(block, count) in postings {
                        *masses.entry(block).or_default() += count.min(seed.occs_in_p as usize) as u64;
                    }
                }
            }
            let mut prefixes: Vec<Vec<(usize, u64)>> = (0..self.layouts.len()).map(|_| Vec::new()).collect();
            for (block, mass) in masses {
                for &(segment, position) in &self.occurrences[block] {
                    prefixes[segment].push((position, mass));
                }
            }
            let prefixes = prefixes
                .into_iter()
                .map(|mut prefix| {
                    prefix.sort_unstable_by_key(|&(position, _)| position);
                    let mut cumulative = 0;
                    for (_, mass) in &mut prefix {
                        cumulative += *mass;
                        *mass = cumulative;
                    }
                    Prefix::Sparse(prefix)
                })
                .collect();
            return (prefixes, visits);
        }
        let mut masses = vec![0u64; self.unique_blocks];
        let mut visits = 0;
        for seed in query.values() {
            if let Some(postings) = self.postings.get(&seed.kmer.h) {
                visits += postings.len();
                for &(block, count) in postings {
                    masses[block] += count.min(seed.occs_in_p as usize) as u64;
                }
            }
        }
        let prefixes = self
            .layouts
            .iter()
            .map(|layout| {
                let mut prefix = Vec::with_capacity(layout.blocks.len() + 1);
                prefix.push(0);
                for &block in &layout.blocks {
                    prefix.push(prefix.last().unwrap() + masses[block]);
                }
                Prefix::Dense(prefix)
            })
            .collect();
        (prefixes, visits)
    }

    fn upper(&self, segment: usize, prefix: &Prefix, from: usize, to: usize, query_size: usize) -> f64 {
        let ends = &self.layouts[segment].ends;
        let first = ends.partition_point(|&end| end <= from);
        let last = ends.partition_point(|&end| end < to) + 1;
        prefix.sum(first, last).min(query_size as u64) as f64 / query_size as f64
    }
}

fn query_info(sketch: &[Kmer]) -> (H2Seed, Vec<QPos>) {
    let mut grouped: FxHashMap<Hash, Vec<Kmer>> = FxHashMap::default();
    for &kmer in sketch {
        grouped.entry(kmer.h).or_default().push(kmer);
    }
    let mut groups: Vec<_> = grouped.into_iter().collect();
    groups.sort_unstable_by_key(|(hash, _)| *hash);
    let mut query = H2Seed::default();
    let mut histogram = Vec::new();
    for (hash, mut group) in groups {
        group.sort_by_key(|kmer| std::cmp::Reverse(kmer.r));
        let count = group.len() as QPos;
        let representative = *group.last().unwrap();
        let positions = group.iter().map(|kmer| kmer.r).collect::<Vec<_>>();
        query.insert(
            hash,
            Seed::new(representative, 0, count, histogram.len() as QPos, positions.into()),
        );
        histogram.push(count);
    }
    (query, histogram)
}

#[derive(Default, Debug)]
struct Stats {
    nodes: usize,
    pruned_buckets: usize,
    scored_buckets: usize,
    admitted: usize,
    bound_visits: usize,
    bound_secs: f64,
    search_secs: f64,
    exhaustive_secs: f64,
}

fn fingerprint(digest: &mut blake3::Hasher, segment: usize, bucket: usize, mapping: &Mapping) {
    digest.update(format!("{segment}:{bucket}:{mapping:?}\n").as_bytes());
}

fn audit(
    index: &SketchIndex,
    factored: &FactoredIndex,
    sketch: &[Kmer],
    read_length: i32,
    k: i32,
    threshold: f64,
    bounds: Bounds,
) -> Result<Stats> {
    ensure!(
        sketch.len() >= 5,
        "shadow audit requires at least five query sketch entries"
    );
    let window = sketch.len();
    let (query, original_hist) = query_info(sketch);
    let mapper = SHMapper::<false, false, false>::new(index);
    let mut histogram = original_hist.clone();
    let clock = Instant::now();
    let (prefixes, visits) = factored.query_prefixes(&query, bounds);
    let mut stats = Stats {
        bound_visits: visits,
        bound_secs: clock.elapsed().as_secs_f64(),
        ..Stats::default()
    };
    let mut candidate_hash = blake3::Hasher::new();
    let mut oracle_hash = blake3::Hasher::new();
    for (segment_id, segment) in index.segments.iter().enumerate() {
        let length = segment.kmers.len();
        if length == 0 {
            continue;
        }
        let bucket_count = length.div_ceil(window);
        let score = |bucket: usize, histogram: &mut [QPos]| {
            mapper.best_fixed_length(
                segment,
                (bucket * window) as i32,
                ((bucket + 2) * window).min(length) as i32,
                &query,
                histogram,
                read_length - k,
                window as i32,
                Metric::Containment,
                None,
                0.3,
            )
        };
        let clock = Instant::now();
        let mut stack = vec![(0, bucket_count)];
        let mut rejected = Vec::new();
        while let Some((first, last)) = stack.pop() {
            stats.nodes += 1;
            let upper = factored.upper(
                segment_id,
                &prefixes[segment_id],
                first * window,
                ((last + 1) * window).min(length),
                window,
            );
            if upper < threshold {
                stats.pruned_buckets += last - first;
                rejected.push((first, last, upper));
            } else if last - first == 1 {
                stats.scored_buckets += 1;
                let mapping = score(first, &mut histogram);
                ensure!(mapping.score() <= upper, "unsafe surviving leaf bound");
                ensure!(histogram == original_hist, "candidate histogram not restored");
                if mapping.score() >= threshold {
                    stats.admitted += 1;
                    fingerprint(&mut candidate_hash, segment_id, first, &mapping);
                }
            } else {
                let middle = first + (last - first) / 2;
                stack.push((middle, last));
                stack.push((first, middle));
            }
        }
        stats.search_secs += clock.elapsed().as_secs_f64();
        let clock = Instant::now();
        let mut rejected_cursor = 0;
        for bucket in 0..bucket_count {
            let mapping = score(bucket, &mut histogram);
            ensure!(histogram == original_hist, "oracle histogram not restored");
            while rejected_cursor < rejected.len() && rejected[rejected_cursor].1 <= bucket {
                rejected_cursor += 1;
            }
            if let Some(&(first, last, upper)) = rejected.get(rejected_cursor)
                && first <= bucket
                && bucket < last
            {
                ensure!(
                    mapping.score() <= upper,
                    "unsafe bound at segment {segment_id}, bucket {bucket}"
                );
            }
            if mapping.score() >= threshold {
                fingerprint(&mut oracle_hash, segment_id, bucket, &mapping);
            }
        }
        stats.exhaustive_secs += clock.elapsed().as_secs_f64();
    }
    ensure!(
        candidate_hash.finalize() == oracle_hash.finalize(),
        "qualifying leaf mappings changed"
    );
    Ok(stats)
}

fn main() -> Result<()> {
    let args = Args::parse();
    ensure!(
        args.block_size > 0 && args.limit > 0,
        "block size and limit must be positive"
    );
    ensure!((1..=63).contains(&args.k), "k must be between 1 and 63");
    ensure!(
        args.density.is_finite() && args.density > 0.0 && args.density <= 1.0,
        "invalid density"
    );
    ensure!(
        args.threshold.is_finite() && args.threshold > 0.0 && args.threshold <= 1.0,
        "invalid threshold"
    );
    let sketcher = FracMinHash::new(args.k, args.density);
    let clock = Instant::now();
    let mut index = SketchIndex::new();
    let mut reference = needletail::parse_fastx_file(&args.reference)?;
    while let Some(record) = reference.next() {
        let record = record?;
        let sequence = record.seq();
        let length = i32::try_from(sequence.len())?;
        let id = i32::try_from(index.segments.len())?;
        index.segments.push(RefSegment::new(
            sketcher.sketch_into(&sequence, Vec::new()),
            String::from_utf8_lossy(record.id()).into_owned(),
            length,
            id,
        ));
    }
    let reference_secs = clock.elapsed().as_secs_f64();
    let clock = Instant::now();
    let factored = FactoredIndex::build(&index.segments, args.block_size, args.partition);
    eprintln!("partition={:?} bounds={:?}", args.partition, args.bounds);
    eprintln!(
        "shadow-only; no PAF or production pruning; reference_sketch_secs={reference_secs:.6} build_secs={:.6} block_size={} blocks={} distinct_blocks={} tokens={} distinct_tokens={} payload_bytes_lower_bound={}",
        clock.elapsed().as_secs_f64(),
        args.block_size,
        factored.blocks,
        factored.unique_blocks,
        factored.tokens,
        factored.unique_tokens,
        factored.payload_bytes()
    );
    println!(
        "read\tsketch_entries\tdictionary_posting_visits\tnodes\tpruned_buckets\tscored_buckets\tadmitted\tbound_secs\tsearch_secs\texhaustive_secs\taudit"
    );
    let mut reads = needletail::parse_fastx_file(&args.reads)?;
    let mut processed = 0;
    let mut skipped = 0;
    while processed < args.limit {
        let Some(record) = reads.next() else {
            break;
        };
        let record = record?;
        let sequence = record.seq();
        let sketch = sketcher.sketch_into(&sequence, Vec::new());
        processed += 1;
        if sketch.len() < 5 {
            skipped += 1;
            continue;
        }
        let stats = audit(
            &index,
            &factored,
            &sketch,
            i32::try_from(sequence.len())?,
            args.k,
            args.threshold,
            args.bounds,
        )?;
        println!(
            "{processed}\t{}\t{}\t{}\t{}\t{}\t{}\t{:.6}\t{:.6}\t{:.6}\tpass",
            sketch.len(),
            stats.bound_visits,
            stats.nodes,
            stats.pruned_buckets,
            stats.scored_buckets,
            stats.admitted,
            stats.bound_secs,
            stats.search_secs,
            stats.exhaustive_secs
        );
    }
    eprintln!("reads_examined={processed} skipped_short_sketch={skipped}");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn segment(hashes: &[u64], id: i32, offset: i32) -> RefSegment {
        RefSegment::new(
            hashes
                .iter()
                .enumerate()
                .map(|(position, &hash)| Kmer::new(offset + position as i32 * 5, hash, position % 2 == 0))
                .collect(),
            format!("ref{id}"),
            10000,
            id,
        )
    }

    #[test]
    fn factoring_preserves_locations_strands_and_partial_blocks() {
        let mut index = SketchIndex::new();
        index.segments.push(segment(&[1, 2, 3, 4, 1, 2, 3, 4, 9], 0, 25));
        index.segments.push(segment(&[1, 2, 3, 4], 1, 250));
        let mut reversed = segment(&[1, 2, 3, 4], 2, 500);
        for kmer in &mut reversed.kmers {
            kmer.strand = !kmer.strand;
        }
        index.segments.push(reversed);
        index.segments.push(segment(&[], 3, 0));
        let factored = FactoredIndex::build(&index.segments, 4, Partition::Fixed);
        assert_eq!(factored.blocks, 5);
        assert_eq!(factored.unique_blocks, 3);
        assert_eq!(factored.layouts[0].blocks[0], factored.layouts[1].blocks[0]);
        assert_ne!(factored.layouts[1].blocks[0], factored.layouts[2].blocks[0]);
        assert!(factored.layouts[3].blocks.is_empty());
    }

    #[test]
    fn content_boundaries_recover_shifted_repeat_blocks() {
        let hashes: Vec<_> = (1..=64).collect();
        let shifted: Vec<_> = [77, 79].into_iter().chain(hashes.iter().copied()).collect();
        let segments = [segment(&hashes, 0, 25), segment(&shifted, 1, 1000)];
        let fixed = FactoredIndex::build(&segments, 8, Partition::Fixed);
        let content = FactoredIndex::build(&segments, 8, Partition::Content);
        assert_eq!(fixed.blocks, fixed.unique_blocks);
        assert!(content.unique_blocks < content.blocks);
        assert_eq!(content.layouts[0].blocks[1..], content.layouts[1].blocks[1..]);
        assert_eq!(content.layouts[0].ends.last(), Some(&64));
        assert_eq!(content.layouts[1].ends.last(), Some(&66));
    }

    #[test]
    fn factored_search_matches_exhaustive_scoring() {
        let mut index = SketchIndex::new();
        let hashes: Vec<_> = (0..200).map(|position| (position * 17 % 31) as u64).collect();
        index.segments.push(segment(&hashes, 0, 25));
        index.segments.push(segment(&hashes, 1, 500));
        index.segments.push(segment(&[8; 203], 2, 50));
        index.segments.push(segment(&[1, 2], 3, 25));
        index.segments.push(segment(&[], 4, 0));
        for (block_size, partition) in [1, 4, 16, 64, 1024]
            .into_iter()
            .flat_map(|size| [Partition::Fixed, Partition::Content].map(|partition| (size, partition)))
        {
            let factored = FactoredIndex::build(&index.segments, block_size, partition);
            for hashes in [
                vec![8; 5],
                vec![99; 5],
                vec![0, 1, 2, 3, 4],
                (0..70).map(|number| number % 31).collect(),
            ] {
                let sketch = segment(&hashes, 0, 25).kmers;
                for threshold in [0.2, 0.4, 1.0] {
                    let dense = audit(&index, &factored, &sketch, 1000, 25, threshold, Bounds::Dense).unwrap();
                    let sparse = audit(&index, &factored, &sketch, 1000, 25, threshold, Bounds::Sparse).unwrap();
                    assert_eq!(dense.pruned_buckets, sparse.pruned_buckets);
                    assert_eq!(dense.scored_buckets, sparse.scored_buckets);
                    assert_eq!(dense.admitted, sparse.admitted);
                    assert_eq!(dense.nodes, sparse.nodes);
                    assert!(sparse.scored_buckets + sparse.pruned_buckets > 0);
                    if hashes[0] == 99 {
                        assert_eq!(sparse.scored_buckets, 0);
                    }
                }
            }
        }
    }
}
