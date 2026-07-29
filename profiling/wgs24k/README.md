# 125 000 x 24 kbp whole-genome sweep

Built to close a gap: every other whole-genome dataset in this repo is 12.8 kbp, and the only
24 kbp WGS set (`allchr_real_24kbp`) is **2 000 reads** — about 0.015x coverage, ~93% of which is
indexing. That set cannot say anything about whether 24 kbp reads parallelize, because there is
almost no mapping in it to parallelize.

- **Reads**: 125 000 x 24 000 bp = 3 000 000 000 bp = **0.9624x** of T2T-CHM13, simulated from
  `hs1.fa` by `gen24k.py` (seed 42) with 0.5% substitution noise so per-read mapping cost is
  representative. Headers use the repo's ground-truth convention.
- **Simulated, not real.** Valid for throughput, memory and scaling. **Not** valid for accuracy
  claims — all 125 000 reads map at 94.0% Q60, which reflects the absence of a real error model.
- **Params**: `-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment`, on a2, at `9400936`
  (sharded index build). Output byte-identical across every thread count.

| Threads | wall | speedup | index | map | map speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 38.55 s | 1.00x | 9.3 s | 29.3 s | 1.00x | 2.05 |
| 2  | 23.38 s | 1.65x | 7.8 s | 15.6 s | 1.87x | 1.72 |
| 4  | 18.98 s | 2.03x | 7.9 s | 11.1 s | 2.64x | 1.73 |
| 8  | 12.22 s | 3.15x | 6.0 s | 6.2 s | 4.68x | 1.77 |
| 16 | **9.98 s** | **3.86x** | 6.4 s | 3.6 s | **8.07x** | 1.82 |
| 32 | 10.07 s | 3.83x | 6.0 s | 4.1 s | 7.19x | 1.94 |
| 64 | 9.88 s | 3.90x | 5.9 s | 4.0 s | 7.41x | 2.11 |

**The mapper scales 8.07x. The whole run reaches 3.90x, and the entire difference is the index
build**, which sits at a ~6 s floor set by the single-threaded FASTA reader (`index_reading`,
4.2-5.6 s regardless of `-@`). At 16 threads mapping is 3.6 s against 6.4 s of indexing — 64% of
the wall is a phase that no longer responds to core count.

So "whole-genome 24 kbp doesn't parallelize" is not a property of the mapper or of the read
length. It is the fixed index cost, and on this dataset it dominates because 1x coverage is only
~29 s of mapping to begin with.

Per-read cost is 207 µs at 24 kbp against ~160 µs at 12.8 kbp on the real HiFi sets — sublinear in
read length, as expected since the bucket space scales with the read's own half-length.

## Files

- `gen24k.py` — the generator (deterministic, seed 42)
- `results.csv`, `t*.json` (`-x` reports), `t*.time`, `sweep.sh`, `sweep.log`
