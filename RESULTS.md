# shmap-rs results

**The single place to look for benchmark numbers.** Everything here was measured on the
benchmark host `a2` (64-core AVX-512, 376 GB RAM, Ubuntu 24.04, idle) at commit `8bc38f1`,
against T2T-CHM13 (`hs1.fa`, 3 117 292 070 bp, 25 segments), with parameters
`-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3` unless stated otherwise.

`profiling/*/` holds raw artifacts only — `-x` JSON reports, `/usr/bin/time -v` records and the
driver scripts. Findings live here.

Contents, in fixed order:
[1 Summary](#1-summary) ·
[2 Versus the C++](#2-versus-the-c) ·
[3 Thread scaling](#3-thread-scaling) ·
[4 Coverage scaling](#4-coverage-scaling) ·
[5 Stage breakdown](#5-stage-breakdown) ·
[6 Datasets](#6-datasets) ·
[7 Correctness](#7-correctness)

---

## 1 Summary

| | |
|---|---|
| Speed vs C++ `shmap`, single-threaded | **2.13-2.31x** (all three metrics, real long reads) |
| Speed vs C++ at `-@ 4` | **5.13-6.35x** (the C++ cannot use more than one core) |
| Peak memory | **~1.9-2.3 GB vs 18.85 GB — 8-10x less** |
| Best whole-run thread speedup | **13.63x** (`-@ 32`, Jaccard, 10x coverage) |
| Mapper-only thread speedup | ~7.7-11.4x, reached at 16-32 threads |
| Index build | ~2.9-3.4 s, down from ~9.4 s |
| Determinism | output byte-identical across all thread counts, every metric |

The one place the C++ wins: a 101 KB reference, 0.09 s against 0.40 s. That is fixed startup
cost and it is gone by chrY.

---

## 2 Versus the C++

Real HG002 HiFi, 149 438 reads, mean 23 189 bp, 1.1117x coverage (dataset D1 below).
Single-threaded is the like-for-like column — the C++ has no threading. C++ figures are the
median of three runs; run-to-run variance on this host is a few percent.

| metric | shmap-rs `-@1` | shmap-rs `-@4` | C++ | speedup `-@1` | speedup `-@4` | rs RSS | C++ RSS |
|---|---:|---:|---:|---:|---:|---:|---:|
| Containment | 46.18 s | **17.51 s** | 98.30 s | **2.13x** | **5.61x** | 1.90 GB | 18.85 GB |
| Jaccard | 57.17 s | 20.80 s | 132.05 s | **2.31x** | **6.35x** | 2.30 GB | 18.85 GB |
| bucket_SH *(no refinement)* | **38.59 s** | **16.35 s** | 83.92 s | **2.17x** | 5.13x | 2.10 GB | 18.85 GB |

Accuracy at the same settings — both implementations map identically many reads:

| metric | mapped | mapq 60 | mapq 0 | agreement with C++ (12 core PAF cols) |
|---|---:|---:|---:|---:|
| Containment | 149 194 | 139 309 | 9 851 | 146 681 / 149 194 = **98.32%** |
| Jaccard | 146 120 | 138 421 | 7 663 | 145 557 / 146 120 = **99.61%** |
| bucket_SH | 149 236 | 138 250 | 10 936 | 146 440 / 149 236 = **98.12%** |

Disagreements are almost entirely coordinate-only on the same target (2 482 of 2 513 under
Containment; 31 differ in target, 0 in mapq). That is the documented consequence of shmap-rs
using a stable sort where the C++ uses `std::sort`, so adjacent-bucket ties resolve differently.

**Reading the metrics.** Jaccard is 24% slower than Containment but not because of the formula —
it divides by `m + s_sz - intersection` instead of `m`, so every score is lower, `thr` rises more
slowly through the bucket sweep, pruning is weaker, and **2.7x more buckets reach `refine`**
(1 676 532 against 620 342). It is also the strictest: fewest mapped, fewest mapq 0, highest
agreement with the C++. Dropping refinement entirely (`bucket_SH`) buys only 16% of wall, because
indexing and `match_seeds`/`sketching` are untouched — and it costs 1 059 reads at Q60 while
raising mapq 0 by 1 085. Refinement is what separates confident from ambiguous.

---

## 3 Thread scaling

Output is **byte-identical across every thread count** on all nine combinations. Best per column
in bold. From `profiling/sweep_metrics/` (63 runs).

### 24 kbp reads, 0.9624x — 125 000 reads (dataset D2)

| threads | Containment | | Jaccard | | bucket_SH | |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 38.75 s | 1.00x | 50.79 s | 1.00x | 35.78 s | 1.00x |
| 2 | 25.43 s | 1.52x | 31.24 s | 1.63x | 23.46 s | 1.53x |
| 4 | 16.44 s | 2.36x | 20.11 s | 2.53x | 16.64 s | 2.15x |
| 8 | 11.08 s | 3.50x | 12.33 s | 4.12x | 10.27 s | 3.48x |
| 16 | **8.29 s** | **4.67x** | 9.00 s | 5.64x | **8.87 s** | **4.03x** |
| 32 | 8.58 s | 4.52x | **8.44 s** | **6.02x** | 9.04 s | 3.96x |
| 64 | 9.22 s | 4.20x | 9.01 s | 5.64x | 9.23 s | 3.88x |

### 12.8 kbp real HiFi, 0.999x — 242 534 reads (dataset D3)

| threads | Containment | | Jaccard | | bucket_SH | |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 54.38 s | 1.00x | 67.02 s | 1.00x | 46.18 s | 1.00x |
| 2 | 35.95 s | 1.51x | 40.30 s | 1.66x | 28.92 s | 1.60x |
| 4 | 21.39 s | 2.54x | 23.94 s | 2.80x | 19.82 s | 2.33x |
| 8 | 13.49 s | 4.03x | 14.90 s | 4.50x | 11.97 s | 3.86x |
| 16 | **11.69 s** | **4.65x** | 11.37 s | 5.89x | **9.51 s** | **4.86x** |
| 32 | 11.86 s | 4.59x | **10.47 s** | **6.40x** | 11.84 s | 3.90x |
| 64 | 11.68 s | 4.66x | 11.91 s | 5.63x | 12.30 s | 3.75x |

### 12.8 kbp real HiFi, 9.987x — 2 425 341 reads (dataset D4)

| threads | Containment | | Jaccard | | bucket_SH | |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 423.06 s | 1.00x | 572.59 s | 1.00x | 358.22 s | 1.00x |
| 2 | 235.68 s | 1.80x | 300.94 s | 1.90x | 193.64 s | 1.85x |
| 4 | 136.11 s | 3.11x | 176.32 s | 3.25x | 117.67 s | 3.04x |
| 8 | 73.37 s | 5.77x | 92.63 s | 6.18x | 63.15 s | 5.67x |
| 16 | 45.45 s | 9.31x | 51.51 s | 11.12x | **45.01 s** | 7.96x |
| 32 | **43.41 s** | **9.75x** | **42.00 s** | **13.63x** | 44.44 s | **8.06x** |
| 64 | 45.54 s | 9.29x | 44.09 s | 12.99x | 46.54 s | 7.70x |

Whole-run scaling is best at depth simply because there is enough mapping work to bury the fixed
index cost — indexing is ~9% of the wall at `-@ 32` on 10x, against ~50% for the 24 kbp 1x run.
The mapper's own ceiling is a consistent ~7.7-11.4x in all three, reached at 16-32 threads.

---

## 4 Coverage scaling

Nothing degrades at depth. 1x → 100x on a repeated whole-genome set (see D5 for why it is
repeated), `-@ 8`, Containment.

| depth | reads | read bases | wall | indexing | peak RSS | reads/s |
|---|---:|---:|---:|---:|---:|---:|
| 1x | 242 845 | 3.1 Gbp | 21.7 s | 9.8 s | 2.13 GB | 20 378 |
| 10x | 2 428 450 | 31.2 Gbp | 114.8 s | 10.2 s | 2.18 GB | 23 212 |
| 30x | 7 285 350 | 93.5 Gbp | 314.2 s | 10.0 s | 2.16 GB | 23 950 |
| **100x** | **24 284 500** | **311.7 Gbp** | 1034.4 s | 9.9 s | **2.16 GB** | 23 703 |

Throughput holds in a ±1.5% band from 10x to 100x, and **peak memory rises 1.4% for a hundredfold
increase in input**. Per-read work counters are identical to two decimals at every depth, which
confirms the per-read state reset holds across 24.3 M reads. Nothing overflowed — the largest
counter reached 1.74e12 against an `i64` budget.

---

## 5 Stage breakdown

Shares of `query_mapping`, single-threaded, Containment.

### By read length — longer reads are cheaper per base

| stage | 23.2 kb | 12.8 kb |
|---|---:|---:|
| `match_rest` | 32.2% | 26.6% |
| ⌐ `refine` | 19.0% | 14.7% |
| `prepare` | 24.1% | 20.4% |
| ⌐ `collect_kmer_info` | 15.9% | 13.3% |
| `match_seeds` | **20.6%** | **32.3%** |
| `sketching` | 20.4% | 15.7% |
| `bucket_merge` | 2.6% | 4.7% |

Per-read cost is 220 µs at 23.2 kb against 165 µs at 12.8 kb: **1.81x the length for 1.33x the
cost, ~26% cheaper per base**. Bucket width is the read's own half-length, so a longer read
partitions the reference into fewer, wider buckets — 194 seeded buckets/read against 252, despite
carrying 231 k-mers against 128.

### By metric — what refinement costs

| stage | Containment | Jaccard | bucket_SH |
|---|---:|---:|---:|
| `refine` | 5.7 s | **15.1 s** | 0.1 s |
| `match_rest` | 10.1 s | 21.4 s | 3.6 s |
| `query_mapping` | 32.3 s | 43.9 s | 25.8 s |
| buckets refined | 620 342 | **1 676 532** | 384 863 |

### Indexing — three serial floors removed in turn

| threads (10x run) | 1 | 2 | 4 | 8 | 16 | 32 | 64 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `indexing` (wall) | 9.6 | 7.3 | 4.6 | 3.6 | **2.9** | 3.7 | 3.4 |
| `index_reading` | 4.4 | 2.9 | 4.1 | 4.3 | 2.9 | 3.2 | 3.4 |
| `index_initializing` | 0.0 | 4.5 | 2.3 | 1.5 | 1.5 | 1.9 | 1.8 |
| `index_finalizing` | 0.7 | 0.7 | 0.2 | 0.2 | 0.2 | 0.2 | 0.1 |

`index_initializing` was flat at ~8.4 s from 1 thread to 64; sharding the index by k-mer hash put
it at 1.5-1.9 s. `index_reading` then became the floor at 4.3-5.6 s; parsing byte ranges in two
passes put it at 1.5-1.7 s. Indexing bottoms out near **2.9-3.4 s, down from ~9.4 s**, with no
single dominant phase — both remaining pieces are within ~2x of the 0.87 s needed merely to stream
the 3.18 GB reference off page cache. Further gains need *reading less*, not more parallelism.

---

## 6 Datasets

| id | reads | mean len | count | bases | coverage | real? |
|---|---|---:|---:|---:|---:|---|
| D1 | HG002 HiFi, GIAB 20 kb library, filtered ≥22 kb | 23 189 bp | 149 438 | 3.47 Gbp | 1.1117x | **real** |
| D2 | Simulated from `hs1.fa`, 0.5% substitution noise | 24 000 bp | 125 000 | 3.00 Gbp | 0.9624x | simulated |
| D3 | HG002 HiFi 15 kb library (`hifi_1x.fa`) | 12 838 bp | 242 534 | 3.11 Gbp | 0.999x | **real** |
| D4 | Same, 10x subset (`hifi_10x.fa`) | 12 836 bp | 2 425 341 | 31.1 Gbp | 9.987x | **real** |
| D5 | D3-equivalent repeated N times, streamed via FIFO | 12 837 bp | ≤24.3 M | ≤311.7 Gbp | ≤100x | repeated |

Notes that matter when quoting these:

- **D1 is 23.2 kb, not 24.0.** No real dataset gives both ≥125 000 reads *and* a 24 kb mean: a
  ≥23 kb cut reaches 24.2 kb but yields only ~72 k reads across both 20 kb-library movies. ONT is
  the only source that hits 24 kb at scale, and its ~5-10% error rate makes it incomparable here.
- **D2 is simulated** — valid for throughput, memory and scaling, *not* for accuracy claims.
- **D5 is one read set repeated**, because the GIAB HG002 CCS set is only ~13x in total. So
  `mapped%`/`mapq60%` are trivially constant there and prove nothing; throughput, memory,
  per-read cost and overflow behaviour remain valid.
- `allchr_real_24kbp`, used in older tables, is **not** 24 kbp reads — it is real HiFi at ~13 kb
  and only 2 000 reads (~0.015x). The "24kbp" is the nominal library size.

---

## 7 Correctness

Byte-identical output against a previous build is a regression gate, not a correctness check — it
inherits whatever the baseline had. These run in addition.

| check | result |
|---|---|
| Test suite (release and debug) | **53 / 53** pass |
| Thread-count determinism | identical across `-@ 1..64`, all 3 metrics, all 3 datasets |
| Structural invariants | hold on all metrics (coords in range, mapq 0-60, span sane) |
| Score invariants (`J,J2 ∈ [0,1]`, `J2 ≤ J`, `sh ≥ J`) | hold on all metrics |
| Ground truth, Containment | **99.70%** of simulated reads within one read length of truth |
| Ground truth, bucket_SH | 99.28% — measurably worse without refinement |

Running the debug profile matters: it activates the `debug_assert`s that pin the risky designs —
that `best_fixed_length` restores `diff_hist` exactly (`intersection == 0`), that the parallel
reader's second pass writes precisely what its first pass counted, and that a segment's parts tile
its buffer with no gaps.

### A bug these checks found

Logical validation caught a defect that byte-identical diffing structurally could not, because
both builds shared it. `matches_in_bucket`'s single-hit branch tested position but not `segm_id`,
so a k-mer whose one genome-wide hit lay in a different segment could be folded into a bucket —
contributing a coordinate from the wrong chromosome to `r_min`/`r_max`, and inflating `matches`,
which raises `sh` and so weakens pruning in *every* metric. Under `bucket_SH`, which reports those
coordinates directly, it emitted an end position **1.28 Mb past the end of chr6**:

| read | before | after | read length |
|---|---:|---:|---:|
| `137103051` | span 2 098 835 (past chr6 end) | **23 791** | 23 987 |
| `71696579` | span 2 010 396 | **22 373** | 22 500 |
| `23986424` | span 4 744 828 | **22 852** | 23 095 |

Fixed in `8bc38f1`. This is an upstream bug, faithfully ported — the C++ has the identical
omission at `src/shmap.h:201-207` — and is worth reporting there.
