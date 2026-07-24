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

## Current numbers

| Dataset | Threads | Wall | Peak RSS |
|---|---:|---:|---:|
| chrY_sim_10kbp_10x  |  1 |  91.9s | 0.19 GB |
| chrY_sim_10kbp_10x  | 16 |   6.8s | 0.19 GB |
| chrY_sim_24kbp_10x  |  1 |  23.4s | 0.19 GB |
| chrY_sim_24kbp_10x  | 16 |   2.2s | 0.19 GB |
| allchr_sim_10kbp_1x |  1 |  81.5s | 2.73 GB |
| allchr_sim_10kbp_1x | 16 |  14.6s | 2.44 GB |
| allchr_real_24kbp   |  1 |  10.6s | 2.73 GB |
| allchr_real_24kbp   | 16 |   9.8s | 2.02 GB |

Accuracy identical at every thread count (Mapped Q60: 22918 / 6902 / 228165 / 1876, Wrong Q60 = 0).

## What's optimized

- **`Buckets` storage → sparse `FxHashMap`** instead of a whole-reference-sized `Vec` (was ~15 GB
  per worker thread on the human genome). Fixed the memory blowup and a "gets slower with more
  threads" regression.
- **Buffered stdout** in the collector instead of `print!()` per read.
- **`match_seeds` reuses one scratch map** instead of allocating fresh per multi-hit seed.
- **Reference indexing parallelized** across `-@`, applied in strict file order for determinism.
- **Sketching**: precomputed rolling-hash LUTs (fewer rotates/base) + `Entry` API in k-mer
  indexing (hash once, not twice).

## Remaining bottlenecks

- **`match_seeds`** is the largest per-read hot-path stage — a real CPU profiler (perf/flamegraph)
  would be the next step if mapping throughput itself needs to improve.
- **Sketching** is now memory-latency-bound on the base-by-base rolling hash; a further big win
  would need SIMD or 2-bit-packed sequence encoding (large effort).
- **Indexing parallelism** is capped by the largest single chromosome (Amdahl's law) — helps
  mainly through pipelining, not raw thread scaling.
