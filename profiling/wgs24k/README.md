# 125 000 x 24 kbp whole-genome sweep

> **Superseded by [`RESULTS.md`](../../RESULTS.md).** That file is the single, ordered source for current benchmark numbers. This directory is kept for its raw artifacts (`-x` reports, `/usr/bin/time -v` records, driver scripts) and for the provenance of how its dataset was built. Figures in the prose below may be from an earlier commit.


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
| 1  | 39.43 s | 1.00x | 9.2 s | 30.3 s | 1.00x | 2.18 |
| 2  | 25.65 s | 1.54x | 8.0 s | 17.6 s | 1.72x | 1.78 |
| 4  | 16.22 s | 2.43x | 5.0 s | 11.2 s | 2.70x | 1.84 |
| 8  | 10.02 s | 3.94x | 3.5 s | 6.5 s | 4.67x | 2.02 |
| 16 | 7.38 s | 5.34x | 3.5 s | 3.9 s | 7.83x | 2.05 |
| 32 | 7.62 s | 5.17x | 3.8 s | 3.8 s | **7.98x** | 2.28 |
| 64 | **7.10 s** | **5.55x** | 3.2 s | 3.9 s | 7.69x | 2.36 |

**The mapper scales ~7.9x. The whole run reaches 5.55x, and the difference is the index build.**
At 16 threads mapping is 3.9 s against 3.5 s of indexing, so the two are now about equal. Earlier
generations of this table read 9.98 s / 3.86x (serial reader) and 8.38 s / 4.66x (one-pass parallel
reader) at `-@ 16`; the current numbers are with the two-pass reader.

Per-read cost is ~207 µs at 24 kbp against ~160 µs at 12.8 kbp on the real HiFi sets — sublinear
in read length, as expected since the bucket space scales with the read's own half-length.

The full three-dataset sweep this belongs to is in `../final_sweep/`.

## Files

- `gen24k.py` — the generator (deterministic, seed 42)
- `results.csv`, `t*.json` (`-x` reports), `t*.time`, `sweep.sh`, `sweep.log`
