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
comparison on their own. Same datasets/params, everything at `-@ 1`.

Provenance, because the columns are not all equally fresh: **both shmap-rs columns were measured
for this comparison**, back-to-back on an otherwise idle benchmark host (`/usr/bin/time -v`,
load average ~0 throughout). The **C++ `shmap` column is the repo's stored `results_rw` run**, not
re-run alongside them.

#### HiFi — 6 000 reads vs whole T2T-CHM13, `-k 15 -r 0.0625 -t 0.20 -m Containment`

| | C++ `shmap` | shmap-rs *before* | shmap-rs *after* |
|---|---:|---:|---:|
| **wall** | 2637 s | 1995.6 s | **685.9 s** |
| speedup vs C++ | 1.00x | 1.32x | **3.85x** |
| speedup vs previous shmap-rs | — | 1.00x | **2.91x** |
| **peak RSS** | 13.5 GB | 7.67 GB | **7.06 GB** |
| mapped | 5991 | 5991 | 5991 |
| mean mapq | 57.0 | 57.0 | 57.0 |
| Q60 / mapq0 | — | 5689 / 296 | 5689 / 296 |

The two shmap-rs builds produce **byte-identical PAF** — 0 differing lines over 5 991 records, both
files exactly 2 870 609 bytes — so the 2.91x is pure throughput, with no recall or mapq trade.

Where the 1 310 s went (from `-x` stage timers on the same two runs):

| stage | before | after | |
|---|---:|---:|---|
| `bucket_merge` | 1342.1 s | 36.6 s | **-97.3% (36.7x)** |
| `match_seeds` | 618.2 s | 610.0 s | -1.3% |
| `indexing` | 21.0 s | 23.1 s | +2.1 s |

Essentially all of it is `bucket_merge`: the sort-based bucket aggregation was replaced by a dense
accumulator. `match_seeds` is untouched and is now ~92% of mapping — it is the next target.

#### Whole genome, real HG002 24 kbp — 2 000 reads, `-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3`

The Table-1 dataset, included because it is the case that did *not* speed up:

| | C++ `map-shmap` | shmap-rs *before* | shmap-rs *after* |
|---|---:|---:|---:|
| **wall** | 32.9 s | 11.63 s | 11.70 s |
| **peak RSS** | 18.9 GB | 2.86 GB | **2.56 GB** |
| Q60 correct | 1876 | 1876 | 1876 |

Best-of-3 each. This run is ~90% indexing, so the mapping win barely applies and it pays slightly
for shrinking the index's hit lists: **~1% slower for 11% less memory**, output identical. At
`-@ 8` it is ~9% *faster* (11.8-13.5 s -> 10.6-10.9 s) from the chunked-sketching work.

#### Not re-measured

No "after" numbers exist for these yet:

| dataset | C++ `shmap` | shmap-rs (stale figure) |
|---|---:|---:|
| ONT WGS | 7795 s / 13.5 GB | 7920 s / 9.5 GB |
| CLR WGS | 1110 s / 13.6 GB | 809 s / 7.3 GB |
| chrY sim 10 kbp | 110 s / 0.38 GB | 74 s / 0.19 GB |

ONT alone is a ~2.2 h baseline run, and the chrY simulated read sets are not present on the
benchmark host. The two WGS rows are additionally *pre-radix-sort* figures, so they do not reflect
even the previous baseline. ONT and CLR should move in HiFi's direction but likely less far: the
dense accumulator's win scales with how much a read over-touches buckets, and ONT's longer reads
mean a larger half-length and so fewer, more heavily shared buckets. The 4-thread table above is
likewise pre-dense.

### How it got here

Three rounds of fixing the `Buckets` accumulator. An early sparse-`FxHashMap` rewrite (fixing a
~15 GB dense-array blowup) had regressed single-thread speed ~20% below C++ in this k=15 regime; an
append-only buffer merged once per read recovered most of that, and an O(n) radix sort recovered
the rest. The last round removed the merge entirely: a read produces ~4 M raw bucket contributions
but the whole reference only *has* ~242 k buckets at that read's half-length, so shmap-rs now
accumulates straight into a dense ~4 MB (L3-resident) array and reads the sorted result back with a
single linear scan. See `PROFILING.md` for the stage-by-stage breakdown, for the supporting
data-structure changes, and for why this is not a return to the old multi-GB dense array.
