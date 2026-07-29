# C++ `shmap` on the same ladder

Companion to `README.md`, which runs the 1x→100x ladder for shmap-rs against
the whole genome. This file is the head-to-head against the C++ original.

## Why this ladder uses chr21 and not the whole genome

**The C++ cannot run against `hs1.fa` on this host.** Measured directly, not
inferred: building the whole-genome index, the C++ reached **12.80 GB
resident with a further 6.55 GB pushed to swap** — 19.35 GB of demand against
14.3 GB of RAM. (That figure independently corroborates the 19.30 GB the
64-core benchmark host recorded in `../realworld_hifi/`.)

It did not fail, it thrashed. With 20 GB of swap available it kept running, in
uninterruptible I/O wait, accumulating 57,567 major page faults and taking
151 s to get 10% through **2,000** reads — roughly 0.45 s/read against the
0.34 ms/read the benchmark host managed, a ~1300x swap penalty. Extrapolated,
the 1x point alone would need ~30 hours and the 100x point ~125 days. The run
was killed; any timing from it would measure this host's swap device, not the
algorithm.

So the head-to-head is run on **chr21** (45,090,682 bp), where both fit in RAM
comfortably (287 MB / 102 MB), using the chr21-origin reads extracted from the
same simulated set — 3,514 reads, 45,095,498 bases, **1.0001x**. Depth is
reached by repeating that set, as in `README.md`.

Note this makes chr21 an *easier* problem than the whole genome: with a
single-chromosome reference there is no cross-chromosome ambiguity. The
comparison between the two implementations is still apples-to-apples, since
both get exactly the same reference and reads.

## Fairness: the C++ default build is instrumented

`Makefile` line 17 adds `-DTRACY_ENABLE` unconditionally, compiling the Tracy
profiler client into the hot paths. Measured on the 10x point, that costs the
C++ **8.8%** (8.82 s instrumented vs 8.11 s clean).

Every C++ number below is from a **Tracy-free** build, which is the fair
comparison. Anyone reproducing the numbers in `../realworld_hifi/` or
`PROFILING.md` should check whether that binary had Tracy enabled — if it did,
those C++ timings are ~9% pessimistic and the speedups quoted there are
correspondingly generous.

## Results

`-@ 1` is the like-for-like column: the C++ is single-threaded by design and
has no threading flag. Pre-warmed page cache, stdout to `/dev/null`, C++ not
run first at any depth.

| depth | reads | C++ | shmap-rs `-@1` | shmap-rs `-@8` | speedup `-@1` | speedup `-@8` |
|---|---:|---:|---:|---:|---:|---:|
| 1x | 3,514 | 0.79 s | 0.49 s | 0.24 s | **1.61x** | 3.29x |
| 10x | 35,140 | 5.62 s | 3.86 s | 0.89 s | **1.46x** | 6.31x |
| 30x | 105,420 | 16.21 s | 11.46 s | 2.43 s | **1.41x** | 6.67x |
| 100x | 351,400 | 54.35 s | 37.04 s | 8.06 s | **1.47x** | 6.74x |

Peak RSS is flat in depth for both: **C++ 287 MB, shmap-rs 102 MB — 2.82x
less**, at every depth.

Throughput at 100x: C++ 6,466 reads/s, shmap-rs 9,487 (`-@1`) and 43,598
(`-@8`).

**The single-threaded speedup is stable at 1.41-1.61x across a hundredfold
range of input**, and it reproduces the 1.43-1.62x measured on the whole genome
on entirely different hardware (`../realworld_hifi/`). Two independent hosts,
two reference scales, same answer — that is a real property of the
implementation rather than an artifact of either setup.

The 2.82x memory ratio is much smaller than the 7.6-9.2x seen on the whole
genome, and that is expected: the C++'s per-reference overhead is what scales
badly, so a 45 Mbp reference understates the gap. The whole-genome figure is
the one that matters for real use, and it is the reason this ladder had to drop
to chr21 in the first place.

## Output agreement

At 1x, comparing the 12 core PAF columns of all 3,514 records:

- **3,312 identical (94.25%)**
- 202 (5.75%) differ in coordinates only — same target, same mapq
- 0 differ in target, 0 differ in mapq

That matches the cause documented in `../realworld_hifi/`: adjacent-bucket ties
resolved differently because shmap-rs uses a stable sort where the C++ uses
`std::sort`. Both map all 3,514 reads at every depth, and PAF record counts are
identical at 1x/10x/30x/100x.

## Files

- `chr21_results.csv` — the table above, machine-readable
- `chr21_ladder.sh` — first pass (writes real PAF; used for record-count and
  agreement checks)
- `chr21_clean.sh` — the timing pass quoted above (pre-warmed, `/dev/null`)
- `cl_*.time` — `/usr/bin/time -v` records for the timing pass

Reproducing the C++ side needs `git submodule update --init --recursive` in the
C++ tree first (the vendored deps are submodules), then a build with line 17 of
its `Makefile` commented out.
