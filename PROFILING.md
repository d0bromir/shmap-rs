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

- **`Buckets` storage → append-only `Vec` + LSD radix sort**, not a hashmap. An intermediate sparse
  `FxHashMap<BucketLoc, BucketContent>` design (replacing a whole-reference-sized `Vec`, ~15 GB per
  worker thread on the human genome) fixed a memory blowup but made single-thread mapping ~20%
  *slower* than the C++ original on k=15 whole-genome reads: every touch was a full hashmap
  `entry()` (hash + probe + possible resize), and a read there can touch millions of buckets.
  `add_to_pos`/`add_to_bucket` now just push onto a flat `Vec` (no hash), and duplicate locations
  are merged once per read via a 4-pass-max LSD radix sort on a packed `(segm_id, b)` key — the
  pass count is computed per read (skip always-zero high bits) rather than fixed, since `b` is
  usually far smaller than its 32-bit budget. Net: **1.6× faster than the hashmap regression, and
  now 25% faster than the C++ original** on WGS k=15 HiFi `-@1` (1972.7s vs 2637.2s), same memory
  order of magnitude (7.5 GB vs 13.5 GB), byte-identical mapped/mapq.
- **Buffered stdout** in the collector instead of `print!()` per read.
- **`match_seeds` streams multi-hit seeds** into buckets directly (sorted hits → monotonic bucket
  index) instead of a per-seed scratch hashmap + per-hit division.
- **Reference indexing parallelized** across `-@`, applied in strict file order for determinism.
- **Sketching**: precomputed rolling-hash LUTs (fewer rotates/base) + `Entry` API in k-mer
  indexing (hash once, not twice).

## Remaining bottlenecks

- **Bucket merge** (the radix sort above) is now the dominant per-read cost on k=15 whole-genome
  reads (~68% of mapping time, down from ~77% pre-radix) — `match_seeds` itself is only ~23-31%.
  Root cause is volume, not the sort algorithm: a handful of very-repetitive 15-mers can each touch
  millions of scattered genome-wide buckets, so even O(n) work over that many raw touches is slow.
  Reducing volume (rather than the per-touch cost) is the only remaining lever — investigated and
  **rejected** `-M`/`--max_matches` (blacklists over-frequent k-mers at index time) for this: even a
  mild threshold measurably shifted mapq distribution, and an aggressive one dropped reads from
  mapped entirely (300/300 → 279/300 on a test sample). Not safe as a default; the "never degrade
  mapping" gate rules it out unless a future change can bound volume without touching results.
- **Sketching** is memory-latency-bound on the base-by-base rolling hash; a further big win would
  need SIMD or 2-bit-packed sequence encoding (large effort).
- **Indexing parallelism** is capped by the largest single chromosome (Amdahl's law) — helps
  mainly through pipelining, not raw thread scaling.
