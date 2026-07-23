# shmap-rs profiling

Instrumentation: `-x`/`--profile` (`src/profiling.rs`), writing a per-run JSON report. Reproduce:

```
python3 profiling/benchmark.py --datasets all --threads 16 --profile --only shmap-rs
# or directly:
shmap -s ref.fa -p reads.fa -k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment \
    -@ 16 -x --profile-log run.profile.json
```

`profiling/tables.md` (via `profiling/extract_tables.py`) is a full raw dump of every report;
this file is just the summary.

## Current numbers (post-optimization)

| Dataset | Threads | Wall | Peak RSS |
|---|---:|---:|---:|
| chrY_sim_10kbp_10x  |  1 |  91.1s | 0.19 GB |
| chrY_sim_10kbp_10x  | 16 |   6.8s | 0.19 GB |
| chrY_sim_24kbp_10x  |  1 |  23.4s | 0.19 GB |
| chrY_sim_24kbp_10x  | 16 |   2.2s | 0.19 GB |
| allchr_sim_10kbp_1x |  1 |  82.1s | 2.73 GB |
| allchr_sim_10kbp_1x | 16 |  15.2s | 2.55 GB |
| allchr_real_24kbp   |  1 |  10.6s | 2.72 GB |
| allchr_real_24kbp   | 16 |  10.0s | 2.36 GB |

Accuracy identical to baseline at every thread count (Mapped Q60: 22918 / 6902 / 228165 / 1876,
Wrong Q60 = 0). Verified every optimization; mapping output is byte-exact (`golden_paf` test).

## Optimizations applied

- **`Buckets` storage → sparse `FxHashMap`** (was a whole-reference-sized `Vec`, ~14.9 GB/worker).
  Fixed the memory blowup and the "gets slower with more threads" regression.
- **Buffered stdout** in the collector instead of `print!()` per read.
- **`match_seeds` reuses one `BucketsHash` scratch map** instead of allocating fresh per multi-hit
  seed.
- **Reference indexing parallelized** across `-@`, same reader/worker-pool/collector pipeline as
  mapping, applied in strict file order for determinism (segment IDs / `max_matches` capping both
  depend on processing order).
- **Indexing sketching sped up** by precomputing the three fixed per-base rotates (k, 1, k-1) into
  LUTs (removes 3 of 5 rotates/base in the rolling hash), plus the `Entry` API in `populate_h2pos`
  (hash each k-mer once, not twice). Indexing wall dropped ~12-17% on whole-genome sets.

Net effect: `allchr_real_24kbp` at `-@ 16` went from 51.5s (slower than 1 thread) to ~10s (5x).
Whole-genome peak memory dropped from up to 225 GB to ~2.5 GB flat, at any thread count.

## Remaining bottlenecks

- **`match_seeds`** is still the largest per-read hot-path stage — worth a real CPU profiler
  (perf/flamegraph) if mapping throughput itself needs to improve next.
- **Indexing** is now ~9.5s wall for the whole genome (was ~11s), dominated by sketching
  (~9s CPU), which is memory-latency-bound on the base-by-base rolling hash. The cheap wins (LUT
  precompute, Entry API) are done; further significant gains need SIMD/2-bit-packed sketching
  (large effort). Parallelism is also Amdahl's-law-capped by CHM13's largest single chromosome.
- **Collector overhead** still grows proportionally at high thread counts on small/fast datasets
  (up to ~20% of mapping time) — not yet isolated after the buffered-stdout fix.

## Profiler bugs fixed along the way (see git log for detail)

`6a2c417` frozen-timer snapshot bug · `68b4708` added `worker_setup` timer (the allocation above
was previously invisible) · `aa3d8d4` timer-bracket hardening · `5cae452` a worker panic no longer
hangs the whole pipeline (real deadlock, not just a profiler artifact).
