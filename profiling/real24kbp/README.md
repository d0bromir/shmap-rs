# Real long HiFi reads at 1.1x: shmap-rs vs the C++ original

The first benchmark here on **real** whole-genome reads meaningfully longer than
13 kb, at a read count large enough for mapping — not indexing — to dominate.

## Why this dataset had to be built

Two gaps motivated it:

- Every real whole-genome read set in this repo averages ~12.8 kb.
- `allchr_real_24kbp` is **not** 24 kb reads. It is built by
  `20_prep_real_24kbp.sh` from `hifi_sample.fastq` — real HG002 HiFi at ~13 kb
  — and the upstream benchmark's own README lists it as "real (~13 kb)". The
  "24kbp" in the name is the nominal library size, not the read length. It is
  also only 2 000 reads (~0.015x), so ~93% of that run is indexing.

## What this is, and the one place it misses the brief

Real HG002 PacBio HiFi from the GIAB **20 kb-insert** library (chemistry2),
movies `m64011_190830_220126` and `m64011_190901_095311`, streamed from the
GIAB FTP and length-filtered to **>= 22 kb** on the fly by `fetch_reads.sh`
(only kept reads ever hit disk).

| | |
|---|---:|
| reads | 149 438 |
| bases | 3 465 413 132 |
| mean read length | **23 189 bp** |
| coverage of T2T-CHM13 | **1.1117x** |

**These are 23.2 kb reads, not 24.0.** No available real dataset gives both
>= 125 000 reads *and* a 24 kb mean. Measured on this library, the tail is:

| cutoff | yield | mean | est. reads/movie |
|---|---:|---:|---:|
| >= 20 kb | 21.6% | 21.4 kb | 338 k |
| >= 21 kb | 11.2% | 22.3 kb | 176 k |
| **>= 22 kb** | **5.4%** | **23.2 kb** | **84 k** |
| >= 23 kb | 2.3% | 24.2 kb | 36 k |

A >= 23 kb cut reaches 24.2 kb but yields only ~72 k reads across both movies.
>= 22 kb was chosen to clear 125 000 while staying as close to 24 kb as real
HiFi allows. The only source that hits 24 kb at this scale is ONT
(SRR11032657, 7.8% of reads in a 22-28 kb band), whose ~5-10% error rate would
make it incomparable to every other benchmark in this repo.

## Results

Reference `hs1.fa` (3 117 292 070 bp), `-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m
Containment`, on a2 (64-core, 376 GB). Page cache warmed identically before
each run; runs strictly sequential on an idle host. `bench.sh` is the driver.

| mapper | threads | wall | peak RSS | mapped | mapq 60 | vs C++ |
|---|---:|---:|---:|---:|---:|---:|
| C++ shmap | 1 | 97.88 s | 18.85 GB | 149 194 | 139 309 | 1.00x |
| **shmap-rs** | **1** | **45.98 s** | **1.94 GB** | 149 194 | 139 309 | **2.13x** |
| **shmap-rs** | **4** | **18.10 s** | **2.04 GB** | 149 194 | 139 309 | **5.41x** |

- **9.7x less memory** at identical output. The C++ is single-threaded by
  design, so its column is 1 thread by necessity.
- All three map the same 149 194 reads, with the same 139 309 at Q60.
- shmap-rs `-@ 1` and `-@ 4` are **byte-identical**.
- 4 threads give 2.54x over 1. The shortfall from 4x is the fixed index build:
  9.2 s at `-@ 1`, 5.3 s at `-@ 4`, and it never disappears. Mapping CPU is
  220 µs/read at `-@ 1` and 265 µs/read at `-@ 4` — the ~20% inflation across
  4 workers is the memory contention documented in `../full_suite_a2/`.

The 2.13x single-threaded figure sits above the 1.89-2.04x measured on 12.8 kb
reads in `../full_suite_a2/`, i.e. shmap-rs's advantage widens slightly with
read length.

## Agreement between the two implementations

On the 12 core PAF columns: **146 681 / 149 194 identical (98.32%)**.

| difference | count |
|---|---:|
| coordinates only, same target | 2 482 |
| different target | 31 |
| mapq only | 0 |

That is the adjacent-bucket tie-breaking already documented in
`../realworld_hifi/`: shmap-rs uses a stable sort where the C++ uses
`std::sort`. Mean mapq is unchanged and no read is mapped by one and not the
other.

## Files

- `results.csv` — the table above, machine-readable
- `fetch_reads.sh` — streams and length-filters the reads (reproducible)
- `bench.sh` — the exact benchmark driver
- `rs_t1.json`, `rs_t4.json` — full `-x` reports
- `*.time` — `/usr/bin/time -v` records
- `fetch.log`, `bench.log` — run logs
