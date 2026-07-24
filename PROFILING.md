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
| chrY_sim_10kbp_10x  |  1 |  73.5s | 0.19 GB |
| chrY_sim_10kbp_10x  | 16 |   5.6s | 0.19 GB |
| chrY_sim_24kbp_10x  |  1 |  19.2s | 0.19 GB |
| chrY_sim_24kbp_10x  | 16 |   2.0s | 0.19 GB |
| allchr_sim_10kbp_1x |  1 |  81.3s | 2.73 GB |
| allchr_sim_10kbp_1x | 16 |  14.8s | 2.44 GB |
| allchr_real_24kbp   |  1 |  10.6s | 2.73 GB |
| allchr_real_24kbp   | 16 |   9.6s | 2.02 GB |

Accuracy identical at every thread count (Mapped Q60: 22918 / 6902 / 228165 / 1876, Wrong Q60 = 0).

## What's optimized

- **`Buckets` storage → sparse `FxHashMap`** instead of a whole-reference-sized `Vec` (was ~15 GB
  per worker thread on the human genome). Fixed the memory blowup and a "gets slower with more
  threads" regression.
- **Buffered stdout** in the collector instead of `print!()` per read.
- **`match_seeds` streams multi-hit seeds** into buckets directly (sorted hits → monotonic bucket
  index) instead of a per-seed scratch hashmap + per-hit division. ~30% faster `match_seeds`
  (57.8→40.2s on chrY_10kbp -@1), ~20% off total mapping wall on the mapping-dominated sets.
- **Reference indexing parallelized** across `-@`, applied in strict file order for determinism.
- **Sketching**: precomputed rolling-hash LUTs (fewer rotates/base) + `Entry` API in k-mer
  indexing (hash once, not twice).

## Remaining bottlenecks

- **`match_seeds`** is still the largest per-read stage (~40s on chrY_10kbp -@1) — but it's now at
  the correctness-preserving floor: ~2.35B hits streamed, ~17ns each. Cutting further needs an
  algorithmic change (fewer hits, e.g. `-M`) or SIMD, not micro-opt.
- **Sketching** is memory-latency-bound on the base-by-base rolling hash; a further big win would
  need SIMD or 2-bit-packed sequence encoding (large effort).
- **Indexing parallelism** is capped by the largest single chromosome (Amdahl's law) — helps
  mainly through pipelining, not raw thread scaling.
