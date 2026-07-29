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
| 1  | 39.01 s | 1.00x | 9.2 s | 29.8 s | 1.00x | 2.09 |
| 2  | 24.43 s | 1.60x | 6.9 s | 17.6 s | 1.69x | 1.97 |
| 4  | 15.65 s | 2.49x | 5.3 s | 10.4 s | 2.87x | 2.02 |
| 8  | 12.16 s | 3.21x | 6.1 s | 6.1 s | 4.90x | 2.34 |
| 16 | 8.70 s | 4.48x | 4.7 s | 4.0 s | **7.53x** | 2.31 |
| 32 | **8.38 s** | **4.66x** | 4.4 s | 4.0 s | 7.44x | 2.58 |
| 64 | 8.93 s | 4.37x | 4.8 s | 4.1 s | 7.28x | 2.74 |

**The mapper scales 7.53x. The whole run reaches 4.66x, and the difference is the index build.**
At 16 threads mapping is 4.0 s against 4.7 s of indexing, so indexing is over half the wall. These
numbers are from `1f1f521`, with both the sharded index build and the parallel FASTA reader in
place; the first version of this table was taken before the reader was parallelized and read
9.98 s / 3.86x at `-@ 16`.

So "whole-genome 24 kbp doesn't parallelize" is not a property of the mapper or of the read
length. It is the fixed index cost, and on this dataset it dominates because 1x coverage is only
~29 s of mapping to begin with.

Per-read cost is ~207 µs at 24 kbp against ~160 µs at 12.8 kbp on the real HiFi sets — sublinear
in read length, as expected since the bucket space scales with the read's own half-length.

The full three-dataset sweep this belongs to is in `../final_sweep/`.

## Files

- `gen24k.py` — the generator (deterministic, seed 42)
- `results.csv`, `t*.json` (`-x` reports), `t*.time`, `sweep.sh`, `sweep.log`
