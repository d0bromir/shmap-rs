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

> **This 4-thread table predates the dense bucket accumulator.** The single-threaded section below
> is current and measured on all three platforms; these numbers will improve similarly once re-run.

- **Byte-for-byte the same accuracy as the C++ original** (identical mapped count, map%, and mean
  mapq on all three platforms) — the port stays faithful even in this pathological k=15 regime —
  while **2.6–3.0× faster** (4 threads vs single-threaded C++) and ~1.4–1.8× less memory.
- minSHmap (minimizer-based, sparser seeds) is faster on HiFi/ONT, but on the noisy CLR reads its
  extra mappings come at mapq 8.9 vs shmap-rs's 44.5 — i.e. low-confidence.

### Single-threaded (`-@ 1`), apples-to-apples with the C++ original

The C++ `shmap` has no multithreading, so the 4-thread numbers above aren't a fair speed
comparison on their own. Same datasets/params, everything at `-@ 1`, all three platforms.

Provenance: **both shmap-rs columns were measured for this comparison**, back-to-back and
sequentially on an otherwise idle benchmark host (`/usr/bin/time -v`, load average ~0 throughout).
The **C++ `shmap` column is the repo's stored `results_rw/bench_both` run**, not re-run alongside
them. "before" is commit `1de2a54` (the O(n) radix sort); "after" is the dense bucket accumulator.

| dataset | C++ `shmap` | shmap-rs *before* | shmap-rs *after* | vs C++ | vs before |
|---|---:|---:|---:|---:|---:|
| **HiFi** | 2637.2 s / 13.5 GB | 1995.6 s / 7.67 GB | **685.9 s / 7.06 GB** | **3.85x** | **2.91x** |
| **ONT** | 7795.5 s / 13.5 GB | 4782.5 s / 10.96 GB | **1846.6 s / 7.26 GB** | **4.22x** | **2.59x** |
| **CLR** | 1110.1 s / 13.6 GB | 707.2 s / 7.87 GB | **473.6 s / 7.20 GB** | **2.34x** | **1.49x** |

Accuracy is **byte-identical between the two shmap-rs builds on all three platforms** — 0 differing
PAF lines in every case — and matches the C++ original, as it did before:

| dataset | mapped / 6000 | mean mapq | Q60 | mean read length |
|---|---:|---:|---:|---:|
| HiFi | 5991 (99.85%) | 57.0 | 5689 | 12.8 kbp |
| ONT | 5750 (95.83%) | 54.6 | 5227 | 35.4 kbp |
| CLR | 294 (4.90%) | 44.5 | 216 | 3.1 kbp |

#### Where the time went

| dataset | stage | before | after | |
|---|---|---:|---:|---|
| HiFi | `bucket_merge` | 1342.1 s | **36.6 s** | -97.3% |
| | `match_seeds` | 618.2 s | 610.0 s | -1.3% |
| ONT | `bucket_merge` | 2784.4 s | **58.3 s** | -97.9% |
| | `match_seeds` | 1278.1 s | 1076.6 s | -15.8% |
| CLR | `bucket_merge` | 305.0 s | **59.9 s** | -80.4% |
| | `match_seeds` | 319.6 s | 328.5 s | +2.8% |

The striking part is that **`bucket_merge` lands at 37-60 s regardless of platform**, having been
305-2784 s before. The dense accumulator's cost is a function of the bucket space (a property of
the reference and the read's half-length), not of how many contributions get poured into it — so
it stops scaling with the workload's repetitiveness altogether.

That also explains why the three speedups differ so much: they track how `bucket_merge`-dominated
each baseline was (67% of wall for HiFi, 58% for ONT, 43% for CLR), not anything about the datasets
themselves. `match_seeds` is what is left, and it is now 89% / 59% / 73% of mapping respectively.

ONT's **-34% peak RSS** (10.96 -> 7.26 GB) is the largest memory win of the three, and comes from
the same change: its 35 kbp reads generated the biggest per-read contribution buffers, and those
`Vec`s (grown and never shrunk across reads, plus the radix ping-pong copy) are simply gone. The
dense array that replaced them is ~1.4 MB.

### How it got here

Three rounds of fixing the `Buckets` accumulator. An early sparse-`FxHashMap` rewrite (fixing a
~15 GB dense-array blowup) had regressed single-thread speed ~20% below C++ in this k=15 regime; an
append-only buffer merged once per read recovered most of that, and an O(n) radix sort recovered
the rest. The last round removed the merge entirely: a read produces ~4 M raw bucket contributions
but the whole reference only *has* ~242 k buckets at that read's half-length, so shmap-rs now
accumulates straight into a dense, L3-resident array and reads the sorted result back with a single
linear scan. See `PROFILING.md` for the stage-by-stage breakdown, for the supporting data-structure
changes, and for why this is not a return to the old multi-GB dense array.
