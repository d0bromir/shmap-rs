# Long-read redesign prototype

## Status

This is an experimental implementation of part of the proposed redesign, not a
completed or validated 10x replacement. The original mapping algorithm remains
the default. Native-only whole-genome measurements on a2 and galaxy completed
on 2026-09-12/13 at commit `778d519f001d`: 240 invocations, all passing the
driver's structural and determinism checks, with normalized PAF hashes matching
across architectures. These are experimental mode comparisons, not the full
maintained suite gate or a production accuracy claim.

On B04 (10x real HiFi), adaptive mapping with batching and two parser workers
reduced total time at 64 mapping workers from 31.78 to 19.15 seconds on a2
(1.66x) and from 21.68 to 11.47 seconds on galaxy (1.89x). Dense rescue regressed
in every measured configuration; its repeat index alone took 178-395 seconds
and peak RSS reached about 26.6 GiB. ONT and smaller multithreaded workloads also
regressed end-to-end. See the [complete host results](../benchmarks/results/native-778d519f001d/README.md)
for all configurations, placement counts, provenance, and limitations.

Mapping and rescue are implemented in shmap itself. No external mapper is a
dependency, feature, subprocess, or fallback. The proposed external-mapper
integration was abandoned and its dependency removed before integration.

Implemented:

- Direct bucket jumps instead of advancing through every empty bucket.
- Optional compact index: inline singleton hits and contiguous repeated-hit postings.
- Versioned persistent index with BLAKE3 payload checksum, parameter validation,
  reference size/mtime checks, and optional complete reference hashing.
- Progressive query sampling across eight distributed intervals, initially 128
  bases each and expanding to 256 and 512 bases when necessary.
- Orientation-aware positional candidate voting and local posting-list searches.
- Reuse of verified anchors for approximate coordinates and sampled similarity.
- Optional regional dense repeat sketches and native candidate comparison, with
  independent variant evidence required across multiple query regions.
- Candidate-local dense sketch scans in place of repeated global posting lookups.
- Conservative rescue through the original mapper when evidence is insufficient,
  repetitive, inconsistent, or exceeds the candidate/work budget. This is not
  a guarantee that every false candidate will be detected.
- Reusable worker diagnostics and bounded batched dispatch/result collection.
- Optional record-boundary-parallel parsing for uncompressed FASTA, with ordered
  delivery and the original reader retained for other input formats.
- Rejection of profile archives whose version or date disagrees with their manifest.
- A repeated, interleaved local benchmark with truth, CPU, memory, and PAF checks.

Not implemented or validated:

- Seed-pair/minimizer-tuple indexing and general repeat-rescue accuracy validation.
  Frequent seeds remain available to verification and original-mapper rescue.
- A robust noisy-ONT seed policy, base-level alignment, or split-read output.
- Exhaustive alternative-locus discovery or calibrated MAPQ for sampled mappings.
- Memory-mapped cache loading, posting-block skip directories, numeric counter
  storage, or NUMA-specific scheduling.
- Cross-individual accuracy, cold-cache performance, or 10x acceleration.

## Long-Read Optimization Roadmap

Priority is **long-read mapping throughput**, especially B04 (2,425,341 real
HiFi reads, 10x depth). Setup-only wins are tracked separately and must not be
reported as mapping gains. The release measurements above remain immutable;
the post-release experiments below are not part of release 1.6.0's host results.

### Implemented and Measured

| Improvement | State | Evidence and limits |
| --- | --- | --- |
| Adaptive positional sampling and native fallback | Released, experimental | Host matrix shows deep-HiFi gains; small multithreaded inputs and ONT regress end-to-end |
| Batching and parallel FASTA query parsing | Released, opt-in | B04/64-worker parsing mode: 1.66x total on a2, 1.89x on galaxy; two extra reader workers; bundled-mode comparison |
| Compact/checksummed persistent index | Released, optional | Cache reuse not measured in the host matrix; uncached conversion adds setup cost |
| Dense regional repeat rescue and candidate-local scan | Released, experimental | Only four extra correct B02 placements; repeat-index build takes 178-395 seconds; not a speed recommendation |
| Exact compact storage preallocation | Implemented after 1.6.0; bundled host comparison complete | One-million-key local conversion median 0.318 to 0.268 seconds, 1.18x; seven alternating repeats, same test binary; not a mapping benchmark |
| Eliminate sorting of ordered sampled anchors | Implemented after 1.6.0; no isolated host mapping gain established | Local 100,000-buffer benchmark: 19.68 to 16.39 ms, 1.20x; seven alternating repeats; includes buffer filling; not whole-read or WGS speedup |
| Reuse sparse sample postings during candidate verification | Implemented after 1.6.0; host parity passed, mapping gain inconclusive | Same-binary adaptive-attempt median 1.568 to 1.450 seconds, 1.08x; seven alternating CPU-pinned repeats; noisy, excludes original-mapper fallback and the CLI pipeline |

The anchor change preserves candidate discovery, work limits, evidence thresholds,
nearest-hit choice, scoring, and ties. For reads of at least 4096 bases, all
128/256/512-base sampling windows are disjoint and ordered. Each sample adds at
most one anchor. Forward anchors therefore already have strictly increasing
query positions; reverse anchors need reversal, not sorting. Tests compare the
result with the old tuple sort across lengths, k values, densities, strands, and
missing-anchor patterns. Dense candidate ordering is unchanged.

The compact change sizes the destination hash table and repeated-hit array from
the existing shards before conversion. It preserves posting order and index
contents; parity tests cover empty/single/multi-hit indexes, repeated conversion,
and cached/default PAF equality. It remains secondary to mapping work.

The posting change retains borrowed immutable hit slices from the voting pass
in reusable worker storage. Candidate verification uses those slices instead of
repeating the hash lookup for every sample and candidate. No posting lists are
copied or filtered, and all candidate comparisons and hit-budget accounting are
preserved. Storage grows with the sampled seed count (one slice per sample), not
the number of reference hits. The old path is instantiated only by tests through
a compile-time parameter; there is no new runtime option or external dependency.

The local benchmark performs 32,768 complete adaptive attempts per sample over
128 mutated 12 kb reads on both strands, alternating unique and duplicated
references. Each compact table has one million decoy singleton keys; these
enlarge the table but do not reproduce whole-genome posting distributions or
cache behavior. Both variants placed exactly 16,384 reads in every repeat.
Lookup times ranged from 1.459 to 1.953 seconds and reuse from 1.268 to 1.659
seconds. A shorter unpinned run was noisier still (1.04x median). Treat the 1.08x
result as screening evidence, not an established end-to-end speedup.

A direct regression test compares placements, retained candidates, work counters,
and fallback readiness with the previous lookup path across sharded/compact
indexes, repeated worker reuse, lengths, mutations, strands, thresholds, and
invalid sequence. On the existing 10,000-read synthetic corpus, normalized CLI
PAF and adaptive counters remain identical to the pre-reuse build for plain and
adaptive modes at one and four workers: 9,495 mapped, 8,976 correct, and 6,787
adaptive fast placements. These single CLI runs establish parity only. Their
local artifacts are in `target/long-read-posting-reuse/` (not committed; removed
by `cargo clean`).

### Completed Two-Host Validation

The [archived comparison](../benchmarks/results/native-compare-6e33e5c/README.md)
measured commit `6e33e5c` against release 1.6.0 on a2 and galaxy: 540 native-only
invocations covering B01-B05, default/adaptive/parsing modes, 1/16/64 workers,
and three repeats per revision and host. PAF hashes and adaptive counters match
across revisions, repeats, worker counts, and architectures. B02 retains 125,000
mapped reads and 123,977 truth-overlap-correct placements in every mode.

Across the matrix, adaptive/parsing total-time speedups are 1.096x/1.102x on a2
and 1.173x/1.193x on galaxy. Mapping-only ratios are 1.009x/1.013x and
0.993x/1.010x respectively: no convincing general mapping gain. B04 parsing at
64 workers improves total time from 19.53 to 18.73 seconds on a2 and 11.87 to
10.47 seconds on galaxy; mapping changes from 12.54 to 12.72 and 7.74 to 7.64
seconds. The practical benefit is primarily setup, not the targeted mapping
throughput improvement. Individual configurations can regress.

These are bundled, across-build comparisons, not attribution to one optimization.
Full medians, CPU/RSS, commands, provenance, and raw diagnostics are archived;
the maintained suite and original release results are unchanged. Dense rescue,
short reads, cold caches, and the full maintained acceptance gate were not run.

### Next Priorities

1. **Reduce exact fallback seeding/refinement work on long reads.** B04 parsing
  profiles report 2,004,263 adaptive placements and 421,078 original-mapper
  fallbacks on both hosts. On a2 at 64 workers, summed elapsed intervals include
  140.07 seconds in `match_rest`, 124.96 in `match_seeds`, and 113.33 in `adaptive`;
  these overlap hierarchically and are neither wall time nor process CPU time.
  Investigate reusable range-local evidence and avoiding repeated posting visits
  without discarding repeat copies or altering ambiguity decisions.
2. **Reduce repeated adaptive candidate work.** The first whole-genome posting
  reuse comparison did not establish a general mapping gain; assess a position-aware seed-pair index
  only with bounded memory and complete
  native fallback. Keep thresholds unchanged for exact optimizations; any
  heuristic change needs separate simulated and repeat-rich accuracy gates.
3. **Measure deep-HiFi at 1/16/64 workers before promotion.** Require repeated
  same-binary ablations where feasible, normalized PAF parity, B02 placement
  counts, counters, memory, mapping and full wall time. Count reader workers.
  Validate B01/B03 too; synthetic microbenchmarks are screening tools only.
4. **Make dense rescue affordable before wider use.** Investigate localized or
  reusable repeat indexing, not whole-reference dense work on every invocation.
  Preserve all retained candidate comparisons and account for index creation.
5. **Native noisy-long-read support.** Improve ONT seed survival and split-read
  handling as a separate algorithmic project, with truth and confidence checks.
  Do not improve apparent throughput by mapping fewer reads.

Deferred until profiles justify them: mmap loading, posting-block skip directories,
numeric diagnostic storage, and NUMA scheduling. No external mapper integration is
planned. Broad sparse refinement and frequency blacklisting remain unsuitable
without new evidence; previously measured regressions must not be forgotten.

### Rejected Local Experiment

An exactly boundary-adjusted integer missing-seed limit replaced floating-point
pruning comparisons temporarily. Boundary and PAF tests passed, but five
alternating same-binary runs on the local 10,000-read, 5 Mb synthetic corpus gave
mapping speedups of 0.996x/0.996x for plain mapping at 1/4 workers and 0.964x/1.054x
for adaptive batching. This is not a consistent gain. The production change and
its diagnostic switch were removed; do not describe it as shipped or faster.
Raw local evidence remains in `target/long-read-pruning-ablation/report.json`
(not committed; removed by `cargo clean`).

Reproduce the retained local benchmarks with:

```sh
cargo test --release --lib compact_allocation_benchmark -- --ignored --nocapture
cargo test --release --lib sampled_anchor_order_benchmark -- --ignored --nocapture
cargo test --release --lib adaptive_posting_reuse_benchmark -- --ignored --nocapture
```

For less scheduling noise, prefix the last command with `taskset -c CPU`, choosing
a CPU permitted by the current process's affinity mask. The reported longer run
used the first permitted CPU.

These benchmarks are ignored during normal tests and use the test binary's
allocator. The CLI uses mimalloc, so allocation timings are not CLI guarantees.
None of these local benchmarks establishes a host speedup on its own. The bundled
host result above improves total time but does not approach the 10x goal or
establish a general mapping gain. Further work must target long-read mapping.

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

`--adaptive-dense` adds a separate sketch at density 0.1 over repeat-rich regions.
It requires `--adaptive`, leaves the base sketch unchanged, and adds a reference
read/build cost. Candidates lacking regional coverage, unresolved ties, and
work-budget exhaustion return to the original mapper. Successful dense records
use `am:Z:adaptive-dense-v1` and still have MAPQ 255. Dense rescue is an
accuracy-oriented option, not a general speed recommendation.

`--reader-threads 2` enables two query parser threads, additional to the mapping
workers. It does not change the mapping algorithm or seed selection.

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

### Native dense lookup experiment

After adding dense rescue, the per-candidate verification loop was changed to
scan the candidate's reference sketch sequentially and match against a reusable
query hash table. Duplicate query hashes use linked sample indices; nearest-hit
selection, orientation, tie order, variant evidence, and scoring are unchanged
when neither implementation exhausts its work limit. The scan budget also counts
reference entries examined, so unusually large or repetitive candidates may now
return to the original mapper earlier.

Measured on the same seed-42 synthetic corpus (10,000 reads, 5 Mb reference), one
mapping thread, five interleaved repeats, one binary, dense indexing included:

| Dense verification | Median mapping | Median total | Median CPU |
|---|---:|---:|---:|
| Global posting lookups | 0.945 s | 1.20 s | 1.16 s |
| Candidate-local sketch scan | 0.703 s | 1.00 s | 0.93 s |

This is a **1.34x mapping-phase improvement**, not a 10x result. Mapping-time
ranges were 0.869-0.987 s versus 0.662-0.728 s. Total-time ranges overlapped
(1.00-1.21 s versus 0.80-1.01 s), so the 1.20x total-speedup estimate is noisier.

All ten PAF outputs agreed after removing timing tags: 9,495 mapped reads,
9,000 correctly placed out of 9,500 non-chimeric truth reads, and 1,914 dense
rescues. All 2,000 repeat reads were placed correctly in this synthetic set;
all 500 noisy reads remained unmapped. This does not establish real-genome
repeat accuracy or noisy-read support. The scan and posting methods both used
unavailable MAPQ for the same 8,701 adaptive records.

The report, commands, binary hash, raw profiles, and PAF files are retained in
`target/native-dense-scan-t1/`. Reproduce with:

```sh
python3 benchmarks/scripts/benchmark_adaptive.py \
  --output target/native-dense-comparison --reads 10000 --repeats 5 \
  --threads 1 --dense-lookup-ablation
```

`SHMAP_DENSE_POSTING_LOOKUPS=1` selects the prior loop for diagnostics. The
benchmark controls and records this switch explicitly. The default uses local
scanning; neither variant invokes another mapper.

### Validation

Rust debug/release tests and strict clippy passed during implementation. The
default golden PAF is unchanged. New checks cover strand handling, ambiguous
candidate rescue, compact/cache parity, corrupt caches, parameter mismatch,
partial batches, thread invariance, and profile provenance.
All 71 Rust tests pass in both profiles after the native dense-scan change,
including nearest-anchor parity for both strands and duplicate query seeds.

The native host driver measured B01-B05 with Containment, 1/16/64 mapping workers,
four modes, three repeats on a2 and one on galaxy. It did not run the full
maintained suite, other metrics, cache reuse, or an old-release comparison.
On B02, all modes mapped 125,000 reads. Default and adaptive modes placed 123,977
correctly (true segment/strand and IoU above 0.1); dense rescue placed 123,981.
Both endpoints within 1 kb improved from 97,676 to 117,470 with adaptive mapping
and 117,763 with dense rescue. Adaptive and dense outputs respectively contained
105,203 and 106,669 records with unavailable MAPQ, not confident placements.

During release preparation, the stale current profile archives were restored
from the original `18d0f83627b5` run directories on a2 and galaxy. Recovered
profiles passed version/timestamp checks, and all subject timing rows and stage
TSVs matched the current baselines. The maintained report was regenerated from
that evidence and `report.py --check` passes. No profile version/date was relabeled,
no historical archived run was modified, and the native experiment was not promoted.

Before expanding or promoting this mode, measure real repeat-rich references and
cross-individual truth, validate repeat rescue and implement native ONT/split rescue, calibrate
confidence, and check the full suite. A fast common path with unchanged difficult
reads is not sufficient for the proposed 10x target.