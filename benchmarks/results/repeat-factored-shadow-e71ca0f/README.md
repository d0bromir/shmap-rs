# Repeat-Factored Shadow Prototype

Status: **implemented as a standalone diagnostic, not approved for production**.
No mapper placements, confidence values, or runtime defaults are changed.
Source: [profiling/repeat_factored_probe.rs](../../../profiling/repeat_factored_probe.rs),
registered as the Cargo example `repeat_factored_probe`.

## Implemented

- Intern exact `(hash, strand)` block sequences using full-key equality, not
  fingerprint equality. Reference coordinates and segment adjacency remain in
  the original sketches; shared block evidence never supplies placement coordinates.
- Fixed-size or content-defined partitioning. The latter cuts after hashes
  divisible by the target block size, with a maximum of four times that size,
  allowing shifted repeat copies to regain common boundaries. End fragments
  remain separate unless their full token sequences match.
- Inverted hash-to-distinct-block multiplicities, plus block occurrence lists.
- Dense or sparse per-query prefixes of block overlap bounds. Both account for
  every query hash, duplicate occurrences and every genomic block location.
- Depth-first branch-and-bound over bucket starts, with intervals expanded to
  include every owned two-window bucket. Equality is retained at the threshold.
- Exact leaf scoring through the existing `SHMapper::best_fixed_length`, followed
  by a separate exhaustive audit. Every rejected interval is checked for an
  underestimated score. Hashes of all qualifying `(segment, bucket, Mapping)`
  results must match, including coordinates, strand, score and local statistics.

For each covered block, the prototype sums `min(query_count, block_count)` over
query hashes. It adds these block masses and caps the sum at the query-sketch
size. This is a **looser** bound than clamping each hash once over the entire
interval: repeated evidence in separate blocks can be counted more than once.
It remains conservative because all complete windows lie within the covered
blocks. It does not infer correctness of the unsampled sequence.

## Whole-Reference Screen

a2, complete hs1 reference, **first 16 records** of B04 HiFi, not a random or
representative sample. `k=25`, density 0.01, diagnostic threshold 0.4, target
content block size 16. Three dense/sparse pairs in alternating order, same
release executable, one process, host benchmark lock held during each run.
No external mapper was executed.

| Metric | Result |
| --- | ---: |
| Reference sketch entries | 31,009,328 |
| Block occurrences | 2,018,821 |
| Distinct blocks | 1,855,624 |
| Entries in distinct blocks | 30,014,424 |
| Token reduction from factoring | 3.21% |
| Summary payload lower bound | 689,165,352 bytes |
| Peak process RSS across runs | 4,357,056 KiB |
| Additional dictionary construction | about 11 s per run |
| Exhaustive buckets across 16 queries | 3,873,421 |
| Buckets rejected by bounds | 3,871,691 |
| Exact-scored surviving buckets | 1,730 |
| Qualifying leaf results | 1,697 |
| Bound violations / qualifying-result mismatches | 0 / 0 |

Medians of summed instrumented query intervals across three runs:

| Phase, 16 Queries | Dense Prefix | Sparse Prefix |
| --- | ---: | ---: |
| Bound construction | 0.147748 s | 0.299913 s |
| Tree search and surviving exact scores | 0.044851 s | 0.043240 s |
| Exhaustive audit | 9.744870 s | 9.750890 s |

Sparse prefix construction is about twice as slow here: hashing, expanding and
sorting matched locations outweigh avoiding the dense layout scan. Dense is the
better measured diagnostic configuration. Both remain selectable for ablation;
the command below chooses dense explicitly.

**Do not interpret the exhaustive/search ratio as a mapper speedup.** The existing
mapper already prunes through rarest-first seeding and never exhaustively scores
all these buckets. These interval timings also include audit bookkeeping and
mapping fingerprint construction; they exclude reference setup, query sketching,
query-map preparation, and final mapping/output decisions. The whole executable
also performs the exhaustive audit and deallocates its data. No direct comparison
against the production mapper on this 16-read subset was performed.

Factoring alone removes too little work to support the proposed 10x claim. The
high reject fraction establishes useful bounds on this sample, not economic
candidate search. This layout and the sparse-prefix change are not promoted.
Further investigation must beat the existing seeder, not the exhaustive oracle.
Real-read results here have no origin truth and do not establish Winnowmap accuracy.

## Local Checks

Six fixed/content configurations (target sizes 16/64/256) were also checked on
64 reads from the retained 5 Mb synthetic corpus. All audits passed. Fixed
64-entry blocks shared no blocks; content-defined size 16 reduced stored tokens
by about 7.6%. Every configuration rejected 97.9-99.2% of exhaustive buckets.
These are preliminary screens, not whole-genome compression or speed claims.

Three unit tests cover exact sharing across distinct genomic locations, differing
strands, partial/empty segments, offset-shifted copies, periodic and identical
repeats, absent and duplicate query hashes, thresholds including exact equality,
block-size extremes, and dense/sparse agreement with exhaustive scoring.

## Run

```sh
cargo test --example repeat_factored_probe
cargo test --release --example repeat_factored_probe
cargo build --release --locked --example repeat_factored_probe
/usr/bin/time -v target/release/examples/repeat_factored_probe \
  --reference reference.fa --reads reads.fa --limit 16 \
  --partition content --block-size 16 --bounds dense \
  --k 25 --density 0.01 --threshold 0.4 > shadow.tsv 2> shadow.stderr
```

The executable always audits, outputs TSV rather than PAF, and exits nonzero on
an unsafe rejected bound or changed qualifying result. `--limit` counts input
records; reads with fewer than five sketch entries are counted as skipped in
stderr. Skipping such reads is a diagnostic scope restriction, not mapping policy.
It uses the example's default allocator, not the CLI's mimalloc allocator.

No integration with weighted/Jaccard/absolute-position modes, candidate-order
ties, second-best selection, index persistence, cost-based fallback, or full-read
base-level verification is implemented. The fixed diagnostic threshold is not
claimed to reproduce the production acceptance floor. The proof/audit covers
eligible leaf scores, not final production PAF equivalence after integration.

## Evidence

[report.json](report.json) records the exact source/base-commit/binary hashes,
run order, per-read rows, medians, memory and limitations. `raw-a2.tar.gz`
contains timing, stderr, TSV and hash records for the six final runs. The benchmark
source hash matches the current prototype at measurement time. The base commit is
`e71ca0f548e02e05f6db7d67a7f7297657e3b538`; the prototype is an uncommitted addition,
not part of that commit. Full reference/read files are not copied into the archive.
Input paths identify existing B04 host data; unlike the maintained suite driver,
this standalone probe does not hash-verify those large files against the registry.