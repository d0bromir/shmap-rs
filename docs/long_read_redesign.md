# Long-read redesign prototype

## Status

This is an experimental implementation of part of the proposed redesign, not a
completed or validated 10x replacement. The original mapping algorithm remains
the default. No production accuracy claim follows from the local synthetic test.

Implemented:

- Direct bucket jumps instead of advancing through every empty bucket.
- Optional compact index: inline singleton hits and contiguous repeated-hit postings.
- Versioned persistent index with BLAKE3 payload checksum, parameter validation,
  reference size/mtime checks, and optional complete reference hashing.
- Progressive query sampling across eight distributed intervals, initially 128
  bases each and expanding to 256 and 512 bases when necessary.
- Orientation-aware positional candidate voting and local posting-list searches.
- Reuse of verified anchors for approximate coordinates and sampled similarity.
- Conservative rescue through the original mapper when evidence is insufficient,
  repetitive, inconsistent, or exceeds the candidate/work budget. This is not
  a guarantee that every false candidate will be detected.
- Reusable worker diagnostics and bounded batched dispatch/result collection.
- Rejection of profile archives whose version or date disagrees with their manifest.
- A repeated, interleaved local benchmark with truth, CPU, memory, and PAF checks.

Not implemented or validated:

- Selective dense repeat indexes, seed-pair/minimizer-tuple indexing, or a new
  repeat rescue algorithm. Frequent seeds are not removed from the reference;
  they participate in local verification or original-mapper rescue.
- A robust noisy-ONT seed policy, base-level alignment, or split-read output.
- Exhaustive alternative-locus discovery or calibrated MAPQ for sampled mappings.
- Memory-mapped cache loading, posting-block skip directories, numeric counter
  storage, parallel query parsing, or NUMA-specific scheduling.
- Whole-genome accuracy/performance, cold-cache performance, or 10x acceleration.

## Usage

```sh
cargo build --release
target/release/shmap -s reference.fa -p reads.fa \
  -k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 \
  --adaptive --compact-index --read-batch-size 64 -@ 4 > mappings.paf
```

`--adaptive` requires plain Containment without frequency filtering, rarity
scoring, pruning overrides, absolute-position mode, or verbose truth analysis.
Reads below 4096 bases use the original mapper. Internal work limits route reads
to rescue; they do not declare a truncated candidate set uniquely mapped.

Sampled records use `am:Z:adaptive-v1`, `ss:f:` for sampled seed survival, `na:i:`
for matched anchors, and `ns:i:` for sampled seeds. They intentionally have no
full-sketch `J:f:` tag. Their coordinates and PAF match counts are estimates, not
an alignment. MAPQ is **255 (unavailable)**, not 60; never interpret 255 as high
confidence when filtering or measuring accuracy. Rescued records retain original
mapper semantics. Unsampled sequence can contain variation this prototype misses.

`--index-cache reference.idx` creates a cache when absent, then loads it on later
runs. It implies compact storage. The cache is not overwritten, even if invalid;
use a new path when the reference or sketch parameters change. By default,
reference identity is checked using size and modification time. Add
`--verify-index-reference` for a full content hash check; this reads the reference
and costs time. Cache payload integrity is always checked. Loading currently
checksums and decodes the file rather than memory-mapping it.

Profiling distinguishes `index_load`, `index_save`, and `index_compact` from fresh
indexing. Adaptive counters include `adaptive_fast`, `adaptive_rescue`,
`adaptive_bases`, `adaptive_hits`, and `adaptive_candidates`; `adaptive` includes
both successful attempts and attempts that subsequently require rescue.

## Local experiment

Measured on 2026-09-12 in the local Linux workspace, against an unchanged build of
HEAD `00d2ed8f0d57`. Both binaries use the same host/compiler. This is not host a2
and must not be compared numerically with the published WGS results.

The deterministic seed-42 corpus contains a 5 Mb reference, including two
near-identical 1 Mb regions, and 10,000 reads of approximately 12 or 24 kb:
7,000 unique, 2,000 repeat, 500 noisy, and 500 chimeric reads. Ordinary reads have
0.5% substitutions and 0.1% indels; noisy reads have 6% substitutions. Three
interleaved repeats per mode, page-cache-warmed inputs, PAF written to disk,
profiling enabled. Binary and input hashes, exact commands, individual profiles,
and all PAF outputs are retained by the driver.

| Threads | Mode | Median total | Median mapping | Total speedup |
|---:|---|---:|---:|---:|
| 1 | Unchanged baseline | 1.41 s | 1.268 s | 1.00x |
| 1 | Modified compatibility | 1.41 s | 1.231 s | 1.00x |
| 1 | Compact only | 1.40 s | 1.328 s | 1.01x |
| 1 | Adaptive | 1.01 s | 0.936 s | 1.40x |
| 1 | Adaptive + batches | 0.80 s | 0.618 s | 1.76x |
| 1 | Adaptive + batches + cached index | 0.81 s | 0.610 s | 1.74x |
| 4 | Unchanged baseline | 0.60 s | 0.528 s | 1.00x |
| 4 | Adaptive + batches | 0.40 s | 0.216 s | 1.50x |
| 4 | Adaptive + batches + cached index | 0.40 s | 0.213 s | 1.50x |

The cached row excludes cache creation; the separate cache-creation invocation
(including mapping) took 1.40 s at one thread and 0.61 s at four. Short-run timing
noise remains: cached single-thread total ranged from 0.60 to 0.81 s, and the
four-thread baseline from 0.60 to 0.80 s. These medians are not confidence intervals.

All modes mapped 9,495 reads and placed 8,976 correctly out of 9,500 non-chimeric
truth reads. Correct placement requires the true strand and both endpoints within
one read length of truth. Both endpoints within 1 kb improved from 7,481 to 8,560
with adaptive mapping. All 500 noisy reads remained unmapped, so the experiment
does not validate noisy-read support. No false-confident placement was observed
under the coarse placement definition; 6,787 adaptive records had unavailable
MAPQ and must not be counted as confident. Repeated runs and thread counts had
identical mapping/accuracy counts. No structurally invalid PAF was detected.

Compact storage did not improve uncached speed in this experiment and increased
single-thread peak RSS from approximately 42 MiB to 50 MiB during conversion.
Adaptive batching peaked at approximately 55 MiB (one thread) and 67 MiB (four).
It remains optional; reducing index indirection is not automatically a speed win.

Saved experiment directories:

- `target/adaptive-bench-final-t1/`
- `target/adaptive-bench-final-t4/`

Reproduce with an unchanged baseline binary and a fresh output directory:

```sh
python3 benchmarks/scripts/benchmark_adaptive.py \
  --baseline-binary target/adaptive-baseline/release/shmap \
  --output target/adaptive-experiment --reads 10000 --repeats 3 --threads 1
```

The earlier `target/adaptive-bench-t1/` 2,000-read pilot used a stricter endpoint
definition for its `correct` column; do not compare that column to the final runs.

## Remaining gates

Rust debug/release tests and strict clippy passed during implementation. The
default golden PAF is unchanged. New checks cover strand handling, ambiguous
candidate rescue, compact/cache parity, corrupt caches, parameter mismatch,
partial batches, thread invariance, and profile provenance.

The maintained whole-genome suite could not run: its corpus is not present in
this workspace and `SHMAP_DATA` is unset. Also, `report.py --check` now rejects the
existing x86_64 archive: its 105 JSON profiles identify version 1.3.1 from August
1, while the manifest identifies 1.5.0 from September 12. Matching raw profiles
must be restored or remeasured; neither metadata nor tables were relabeled.

Before expanding or promoting this mode, measure real repeat-rich references and
cross-individual truth, implement and validate repeat/ONT/split rescue, calibrate
confidence, and check the full suite. A fast common path with unchanged difficult
reads is not sufficient for the proposed 10x target.