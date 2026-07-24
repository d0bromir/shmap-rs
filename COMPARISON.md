# shmap-rs vs other mappers (real-world data)

Single-threaded (`-@ 1`), 64-core AVX-512 server. Same datasets/params as Pesho's `shmap` Table 1
(`-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment`). Other mappers' numbers are the stored
Table 1 run (`results/table1_20260718-103540.csv` on the benchmark machine); `map-shmap` is the
original C++ shmap that shmap-rs ports. Time = index + map wall (shmap does both in one pass).
`missed%` = reads not mapped at Q60 (shmap's sketch+threshold design is selective by nature).

### chrY, simulated 10 kbp (48,673 reads)

| mapper | correct Q60 | wrong | missed% | time | mem |
|---|---:|---:|---:|---:|---:|
| **shmap-rs** | **22918** | **0** | 52.9 | **74 s** | **0.19 GB** |
| map-shmap (C++) | 22918 | 0 | 52.9 | 110 s | 0.38 GB |
| blend | 23866 | 191 | 50.6 | 640 s | 0.56 GB |
| winnowmap2 | 44751 | 10 | 8.0 | 28694 s | 10.8 GB |
| minimap2 | 16159 | 0 | 66.8 | 1583 s | 0.62 GB |
| minshmap | 15694 | 0 | 67.8 | 925 s | 0.71 GB |
| mapquik | 0 | 0 | 100 | 17 s | 2.26 GB |

### whole genome, REAL HG002 24 kbp (2,000 reads)

| mapper | correct Q60 | wrong | missed% | time | mem |
|---|---:|---:|---:|---:|---:|
| **shmap-rs** | **1876** | n/a | 6.2 | **11.5 s** | **2.7 GB** |
| map-shmap (C++) | 1876 | n/a | 6.2 | 32.9 s | 18.9 GB |
| blend | 1897 | n/a | 5.2 | 84 s | 7.5 GB |
| winnowmap2 | 1953 | n/a | 2.4 | 356 s | 4.7 GB |
| minimap2 | 1844 | n/a | 7.8 | 150 s | 12.2 GB |
| minshmap | 1838 | n/a | 8.1 | 205 s | 11.0 GB |
| mapquik | 0 | n/a | 100 | 101 s | 5.0 GB |

(Full 4-dataset numbers in `profiling/`. chrY 24 kbp and whole-genome 10 kbp follow the same
pattern.)

## Takeaways

- **Identical accuracy to the C++ original** (`map-shmap`) — same correct-Q60 on every dataset,
  0 wrong — while **1.5–2.9× faster** single-threaded and using **up to ~7× less memory** on the
  whole genome (2.7 GB vs 18.9 GB, from the sparse-`Buckets` rewrite).
- **Fastest of all the correct mappers**, single-threaded: e.g. on real HG002 reads, 11.5 s vs
  84 s (blend) / 150 s (minimap2) / 356 s (winnowmap2), at competitive accuracy and lowest or
  near-lowest memory.
- shmap/shmap-rs trade recall for speed (higher `missed%` on the low-similarity chrY sets);
  winnowmap2 maps more but is 100–380× slower and far heavier. `mapquik` maps nothing at these
  parameters.
- shmap-rs additionally **scales to many threads** (the C++ original is single-threaded) — see
  `BENCHMARKS.md` for up to ~21× at `-@ 32`.

## WGS long reads (minshmap/realworld benchmark)

Real HG002 long reads (6,000 each) mapped against the whole T2T-CHM13 genome, using that
benchmark's params (`k=15`, `r=2/(w+1)=0.0625`, `-m Containment`, dataset-specific `theta`,
4 threads). This is shmap's hardest regime (k=15 makes 15-mers hugely repetitive genome-wide).
`shmap`/`minSHmap` numbers are the repo's stored `results_rw/bench_both` run; script:
`profiling/bench_shmaprs_wgs.py`.

| dataset | mapper | mapped | map% | mapq | time | mem |
|---|---|---:|---:|---:|---:|---:|
| HiFi | **shmap-rs (4t)** | 5991 | 99.85 | 57.0 | **1014 s** | **7.3 GB** |
| HiFi | shmap (C++) | 5991 | 99.85 | 57.0 | 2637 s | 13.5 GB |
| HiFi | minSHmap | 5991 | 99.85 | 55.5 | 325 s | 11.2 GB |
| ONT | **shmap-rs (4t)** | 5750 | 95.83 | 54.6 | **2557 s** | **9.7 GB** |
| ONT | shmap (C++) | 5750 | 95.83 | 54.6 | 7795 s | 13.5 GB |
| ONT | minSHmap | 5655 | 94.25 | 52.8 | 1081 s | 11.2 GB |
| CLR | **shmap-rs (4t)** | 294 | 4.90 | 44.5 | **431 s** | **7.7 GB** |
| CLR | shmap (C++) | 294 | 4.90 | 44.5 | 1110 s | 13.6 GB |
| CLR | minSHmap | 662 | 11.03 | 8.9 | 314 s | 11.2 GB |

- **Byte-for-byte the same accuracy as the C++ original** (identical mapped count, map%, and mean
  mapq on all three platforms) — the port stays faithful even in this pathological k=15 regime —
  while **2.6–3.0× faster** (4 threads vs single-threaded C++) and ~1.4–1.8× less memory.
- minSHmap (minimizer-based, sparser seeds) is faster on HiFi/ONT, but on the noisy CLR reads its
  extra mappings come at mapq 8.9 vs shmap-rs's 44.5 — i.e. low-confidence.

### Single-threaded (`-@ 1`), apples-to-apples with the C++ original

The C++ `shmap` has no multithreading, so the 4-thread numbers above aren't a fair speed
comparison on their own. Same datasets/params, shmap-rs at `-@ 1`:

| dataset | mapper | mapped | mapq | time | mem |
|---|---|---:|---:|---:|---:|
| HiFi | **shmap-rs (1t)** | 5991 | 57.0 | **1973 s** | 7.3 GB |
| HiFi | shmap (C++) | 5991 | 57.0 | 2637 s | 13.5 GB |
| ONT | **shmap-rs (1t)** | 5750 | 54.6 | 7920 s\* | 9.5 GB\* |
| ONT | shmap (C++) | 5750 | 54.6 | 7795 s | 13.5 GB |
| CLR | **shmap-rs (1t)** | 294 | 44.5 | 809 s\* | 7.3 GB\* |
| CLR | shmap (C++) | 294 | 44.5 | 1110 s | 13.6 GB |

\* ONT/CLR predate the buckets radix-sort optimization below and will move similarly to HiFi
(~28% faster) once re-measured; not yet re-run.

Single-threaded shmap-rs on HiFi is now **25% *faster*** than the C++ original (was 3.6% slower
at the start of this round of work), at roughly half the memory — while still scaling further
with threads (the 4-thread table above). Two rounds of fixing the `Buckets` accumulator got here:
an earlier sparse-`FxHashMap` rewrite (fixing a ~15 GB dense-array blowup) had regressed
single-thread speed ~20% below C++ on this k=15 regime; switching to an append-only buffer merged
once per read recovered most of that, and replacing the O(n log n) merge sort with an O(n) radix
sort (dynamic pass count, since bucket indices are usually far smaller than their 32-bit budget)
recovered the rest and then some. See `PROFILING.md` for the stage-by-stage breakdown.
