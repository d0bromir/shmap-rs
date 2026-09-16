# Supported Mapper Speedup, 2026-09-14

Only the original full-read sketch mapper is measured here. No adaptive,
dense-rescue, cache-reuse, or extra parser-worker speedup is included.

## Current Revision

Rust source: `e71ca0f548e02e05f6db7d67a7f7297657e3b538`, version 1.6.0.
One mapping worker, default reader, Containment, `-k25 -r0.01 -t0.4 -d0.075
-o0.3`. Fresh uncached-index runs with warmed input files on a2 and galaxy.
One run per row: these are spot checks, not medians or a new full suite pass.
C++ was **not rerun**: the denominators are the historical same-host C++ times
in [the archived comparison](../native-compare-6e33e5c/versus-cpp.tsv).

| Host | Dataset | Historical C++ Total (s) | Current Rust Total (s) | Rust Mapping (s) | Total Speedup | Further Factor for 10x |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| a2 | B02 simulated 24 kb | 83.87 | 34.08 | 26.234 | 2.46x | 4.06x |
| a2 | B04 real HiFi 10x depth | 890.65 | 380.11 | 372.261 | 2.34x | 4.27x |
| galaxy | B02 simulated 24 kb | 87.04 | 37.22 | 28.699 | 2.34x | 4.28x |
| galaxy | B04 real HiFi 10x depth | 902.68 | 418.32 | 410.778 | 2.16x | 4.63x |

C++ B02 dates are 2026-09-12; B04 dates are 2026-08-10 (a2) and
2026-08-09 (galaxy). The 10x total-time targets for B04 are **89.065 s**
and **90.268 s**, including reference indexing, reading, mapping, and output.
Faster cached starts or more workers are not substitutes for this comparison.

Both hosts report 125,000 B02 mappings, 123,977 correct by the historical
segment/strand plus IoU > 0.1 criterion, and 97,676 with both endpoints within
1 kb. Zero B02 wrong-MAPQ-60 records under that overlap criterion does not
establish calibrated confidence. B04 has 2,419,767 mappings from 2,425,341 real
reads; it has no origin truth, so its zero truth counters are not accuracy results.

All four runs have zero adaptive placements and zero structurally invalid PAF
records. Normalized PAF hashes match across hosts and **all nine older default
runs per dataset/host** (three repeats at 1/16/64 workers). Normalization removes
only the timing tag. This establishes retained output behavior, not Winnowmap
accuracy. The driver's single-run `deterministic` flag alone is not a repeat test.

## Wider Historical Context

Default Rust at `6e33e5c`, three-repeat whole-run medians versus historical C++,
one mapping worker on the same host. These are not new timings of `e71ca0f`.

| Dataset | a2 Default Speedup | galaxy Default Speedup |
| --- | ---: | ---: |
| B01 long HiFi | 2.81x | 2.42x |
| B02 simulated 24 kb | 2.62x | 2.46x |
| B03 real HiFi 1x depth | 2.78x | 2.33x |
| B04 real HiFi 10x depth | 2.41x | 2.13x |
| B05 ONT 24 kb | 2.87x | 2.50x |
| Geometric mean | **2.69x** | **2.36x** |

No rejected-mode bars or ratios contribute to either table. Fresh versus older
Rust differences are not attributable to retirement: single-run noise, host
state, and whole-program code layout remain uncontrolled.

## Bottlenecks

Fresh B04 profiles, summed elapsed stage intervals in seconds:

| Stage | a2 | galaxy | Architectural Cost |
| --- | ---: | ---: | --- |
| `sketching` | 54.90 | 60.78 | Hash all 31.13 billion query bases |
| `prepare` | 55.58 | 69.44 | Group/sort seeds, count index hits, build query map |
| `collect_kmer_info` (inside prepare) | 29.90 | 43.75 | About 299 million unique-seed index lookups |
| `match_seeds` | 83.01 | 85.82 | Expand 4.70 billion hits into overlapping buckets |
| `match_rest` | 112.32 | 123.92 | Repeated seed-range verification and scoring |
| `refine` (inside match_rest) | 70.76 | 83.49 | 12.46 million scored buckets; 9.77 million memo hits |
| `bucket_merge` | 17.78 | 19.51 | 606.69 million accumulated seeded buckets |
| `query_reading` | 25.57 | 25.13 | Parsing, copying and pipeline delivery |
| `indexing` | 7.49 | 7.16 | Whole reference setup, already a small B04 fraction |

Intervals nest and pipeline stages can overlap. They are not mutually exclusive
CPU samples; do not sum the rows or treat `dispatch` as extra mapping work.
No hardware cache-miss counter was collected in these runs: memory-latency claims
come from the access pattern and earlier profiling, not a new perf measurement.

Even optimistically subtracting all current refinement time leaves about
309 s / 335 s for B04, far above the 89 s / 90 s goal. The problem cannot be
solved by one refinement micro-optimization or by removing the ~7 s index build.
See the [lossless redesign proposal](../../../docs/long_read_redesign.md#lossless-coarse-to-fine-search).

## Optimization Screening

Two exact probes were implemented, checked, measured, and removed from production:

| Probe | Alternating Pairs | Total Median Ratio | Mapping Median Ratio | Decision |
| --- | ---: | ---: | ---: | --- |
| Posting-list endpoint rejection before binary search | 5 | 1.000x | 1.044x | No demonstrated total benefit; removed |
| 256-entry refinement lookup ring | 7 | 1.000x | 0.967x | No total benefit, slower mapping median; removed |

Local 5 Mb synthetic reference, 10,000 long reads, one mapping worker, warmed
inputs. Every normalized PAF matched the saved production binary. Range checks
also matched an exhaustive hit oracle; ring checks matched the old scorer across
wraparound, duplicate seeds, both metrics, weighted scores and overlap exclusion.
The independent range regression test remains; rejected production code does not.

The next algorithmic step has a test-only coarse-bound prototype. It compares
region multiset bounds against the actual exhaustive Containment scorer, with
1,769/2,652 synthetic fixture regions below threshold 0.4 and zero underestimated
scores. Omitting the required boundary halo would underestimate 1,149 tested
windows. This is a correctness check, not a speed result or a representative
pruning-rate estimate. Production search is unchanged.

These are noisy, unpinned, across-build screens with fat LTO, **not causal
performance measurements**. They justify not promoting the probes, not universal
claims that the ideas cannot help. Future promotion needs same-binary ablations,
alternating repeated whole-genome runs, memory accounting, and accuracy gates.
Local raw screens are in `target/exact-range-screen/` and
`target/exact-refinement-screen/`; they are not publication evidence of a speedup.

## Provenance

[a2/report.json](a2/report.json) and [galaxy/report.json](galaxy/report.json)
record exact commands, source and binary hashes, registered dataset identities,
wall/mapping/CPU/RSS metrics, and PAF fingerprints. Each host directory includes
`raw-profiles.tar.gz` with the profile JSON, stderr and timing files. Full PAFs
remain on the respective host under
`~/bench-results/supported-e71ca0f-20260914/raw/`.

The existing native driver was run with `--commit e71ca0f548e02e05f6db7d67a7f7297657e3b538
--modes default --only B02,B04 --threads 1 --repeats 1`. It verified registered
datasets, held the host benchmark lock, and built detached worktrees. No external
mapper was executed. No experiments were committed or promoted to the maintained
suite by this measurement.