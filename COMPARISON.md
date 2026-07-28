# shmap-rs vs other mappers (real-world data)

Single-threaded (`-@ 1`), 64-core AVX-512 server. Same datasets/params as Pesho's `shmap` Table 1
(`-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment`). **shmap-rs rows were re-measured with the
current build** (`profiling/table1_t1.csv`); the other mappers' numbers are the stored Table 1 run
(`results/table1_20260718-103540.csv` on the benchmark machine) and were not re-run, since those
tools have not changed. `map-shmap` is the original C++ shmap that shmap-rs ports. Time = index + map wall (shmap does both in one pass).
`missed%` = reads not mapped at Q60 (shmap's sketch+threshold design is selective by nature).

### chrY, simulated 10 kbp (48,673 reads)

| mapper | correct Q60 | wrong | missed% | time | mem |
|---|---:|---:|---:|---:|---:|
| **shmap-rs** | **22918** | **0** | 52.9 | **35.9 s** | **0.13 GB** |
| map-shmap (C++) | 22918 | 0 | 52.9 | 110 s | 0.38 GB |
| blend | 23866 | 191 | 50.6 | 640 s | 0.56 GB |
| winnowmap2 | 44751 | 10 | 8.0 | 28694 s | 10.8 GB |
| minimap2 | 16159 | 0 | 66.8 | 1583 s | 0.62 GB |
| minshmap | 15694 | 0 | 67.8 | 925 s | 0.71 GB |
| mapquik | 0 | 0 | 100 | 17 s | 2.26 GB |

### whole genome, REAL HG002 24 kbp (2,000 reads)

| mapper | correct Q60 | wrong | missed% | time | mem |
|---|---:|---:|---:|---:|---:|
| **shmap-rs** | **1876** | n/a | 6.2 | **11.7 s** | **2.4 GB** |
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
  0 wrong — while **2.8–3.1× faster** single-threaded (110 s → 35.9 s on chrY, 32.9 s → 11.7 s on
  the whole genome) and using **~8× less memory** there (2.4 GB vs 18.9 GB).
- **Fastest of all the correct mappers**, single-threaded: e.g. on real HG002 reads, 11.7 s vs
  84 s (blend) / 150 s (minimap2) / 356 s (winnowmap2), at competitive accuracy and lowest or
  near-lowest memory.
- shmap/shmap-rs trade recall for speed (higher `missed%` on the low-similarity chrY sets);
  winnowmap2 maps more but is 100–380× slower and far heavier. `mapquik` maps nothing at these
  parameters.
- shmap-rs additionally **scales to many threads** (the C++ original is single-threaded) — see
  `BENCHMARKS.md` for up to ~20× at `-@ 32` on chrY. Whole-genome runs plateau around 4× because
  `index_initializing` is still single-threaded.

## WGS long reads (minshmap/realworld benchmark)

Real HG002 long reads (6,000 each) mapped against the whole T2T-CHM13 genome, using that
benchmark's params (`k=15`, `r=2/(w+1)=0.0625`, `-m Containment`, dataset-specific `theta`,
4 threads). This is shmap's hardest regime (k=15 makes 15-mers hugely repetitive genome-wide).
**shmap-rs numbers were measured for this table** (sequentially, on an idle host, `/usr/bin/time
-v`); the `shmap` (C++) and `minSHmap` rows are the repo's stored `results_rw/bench_both` run.
Note the C++ `shmap` has no multithreading, so its column is single-threaded by necessity — see the
apples-to-apples section below. Script: `profiling/bench_shmaprs_wgs.py`.

| dataset | mapper | mapped | map% | mapq | time | mem |
|---|---|---:|---:|---:|---:|---:|
| HiFi | **shmap-rs (4t)** | 5991 | 99.85 | 57.0 | **212.2 s** | **9.2 GB** |
| HiFi | shmap (C++, 1t) | 5991 | 99.85 | 57.0 | 2637 s | 13.5 GB |
| HiFi | minSHmap (4t) | 5991 | 99.85 | 55.5 | 325 s | 11.2 GB |
| ONT | **shmap-rs (4t)** | 5750 | 95.83 | 54.6 | **583.3 s** | **9.1 GB** |
| ONT | shmap (C++, 1t) | 5750 | 95.83 | 54.6 | 7795 s | 13.5 GB |
| ONT | minSHmap (4t) | 5655 | 94.25 | 52.8 | 1081 s | 11.2 GB |
| CLR | **shmap-rs (4t)** | 294 | 4.90 | 44.5 | **154.7 s** | **9.2 GB** |
| CLR | shmap (C++, 1t) | 294 | 4.90 | 44.5 | 1110 s | 13.6 GB |
| CLR | minSHmap (4t) | 662 | 11.03 | 8.9 | 314 s | 11.2 GB |

- **Byte-for-byte the same accuracy as the C++ original** (identical mapped count, map%, and mean
  mapq on all three platforms) — the port stays faithful even in this pathological k=15 regime —
  while **7.2–13.4x faster** and ~1.4x less memory. Output is also identical between `-@ 1` and
  `-@ 4` on all three (0 differing PAF lines), so the threading stays deterministic.
- **shmap-rs is now the fastest of the three on every platform.** This reverses the previous
  finding: minSHmap used to beat it on HiFi (325 s vs 1014 s) and ONT (1081 s vs 2557 s), and now
  trails on both (325 s vs 212 s, 1081 s vs 583 s) — at lower memory (~9.2 GB vs 11.2 GB) and
  without minSHmap's accuracy cost.
- minSHmap (minimizer-based, sparser seeds) still maps more of the noisy CLR reads, but those extra
  mappings come at mapq 8.9 vs shmap-rs's 44.5 — i.e. low-confidence.

#### 4-thread before/after

Same measurement discipline as the single-threaded section: both shmap-rs columns measured
sequentially on an idle host, "before" = commit `1de2a54`.

| dataset | before | after | | RSS before | RSS after | |
|---|---:|---:|---:|---:|---:|---:|
| HiFi | 649.3 s | **212.2 s** | **3.06x** | 9.29 GB | 9.17 GB | -1% |
| ONT | 1750.7 s | **583.3 s** | **3.00x** | **22.46 GB** | **9.06 GB** | **-60%** |
| CLR | 243.3 s | **154.7 s** | **1.57x** | 10.08 GB | 9.17 GB | -9% |

The ONT row is the one to look at. **The old accumulator's memory scaled with thread count** —
10.96 GB at `-@ 1` grew to 22.46 GB at `-@ 4`, because every worker kept its own per-read
contribution buffer plus the radix ping-pong copy, and ONT's 35 kbp reads made those buffers
enormous. The dense accumulator is ~1.4 MB per worker, so the new build is essentially flat across
thread counts (7.25 GB at `-@ 1`, 9.06 GB at `-@ 4`, the difference being index build-up rather
than per-worker state). That removes a real scaling hazard, not just a constant factor: the old
design would have kept growing at 8, 16, 32 threads.

Note also that the previously-published 4-thread figures (HiFi 1014 s / 7.3 GB, ONT 2557 s /
9.7 GB, CLR 431 s / 7.7 GB) predated the O(n) radix sort as well, so they are not the "before"
column here — the measured `1de2a54` baseline above is.

### Single-threaded (`-@ 1`), apples-to-apples with the C++ original

The C++ `shmap` has no multithreading, so the 4-thread numbers above aren't a fair speed
comparison on their own. Same datasets/params, everything at `-@ 1`, all three platforms.

Provenance: **both shmap-rs columns were measured for this comparison**, back-to-back and
sequentially on an otherwise idle benchmark host (`/usr/bin/time -v`, load average ~0 throughout).
The **C++ `shmap` column is the repo's stored `results_rw/bench_both` run**, not re-run alongside
them. "before" is commit `1de2a54` (the O(n) radix sort); "after" is the dense bucket accumulator.

| dataset | C++ `shmap` | shmap-rs *before* | shmap-rs *after* | vs C++ | vs before |
|---|---:|---:|---:|---:|---:|
| **HiFi** | 2637.2 s / 13.5 GB | 1995.6 s / 7.67 GB | **725.6 s / 7.07 GB** | **3.63x** | **2.75x** |
| **ONT** | 7795.5 s / 13.5 GB | 4782.5 s / 10.96 GB | **1932.7 s / 7.25 GB** | **4.03x** | **2.47x** |
| **CLR** | 1110.1 s / 13.6 GB | 707.2 s / 7.87 GB | **491.8 s / 7.20 GB** | **2.26x** | **1.44x** |

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
| HiFi | `bucket_merge` | 1342.1 s | **39.1 s** | -97.1% |
| | `match_seeds` | 618.2 s | 648.2 s | +4.9% |
| ONT | `bucket_merge` | 2784.4 s | **62.2 s** | -97.8% |
| | `match_seeds` | 1278.1 s | 1155.3 s | -9.6% |
| CLR | `bucket_merge` | 305.0 s | **64.0 s** | -79.0% |
| | `match_seeds` | 319.6 s | 339.9 s | +6.4% |

The striking part is that **`bucket_merge` lands at 39-64 s regardless of platform**, having been
305-2784 s before. The dense accumulator's cost is a function of the bucket space (a property of
the reference and the read's half-length), not of how many contributions get poured into it — so
it stops scaling with the workload's repetitiveness altogether.

That also explains why the three speedups differ so much: they track how `bucket_merge`-dominated
each baseline was (67% of wall for HiFi, 58% for ONT, 43% for CLR), not anything about the datasets
themselves. `match_seeds` is what is left, and it is now 92% / 61% / 73% of mapping respectively.

ONT's **-34% peak RSS** (10.96 -> 7.25 GB) is the largest memory win of the three, and comes from
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
