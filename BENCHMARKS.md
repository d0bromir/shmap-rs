# shmap-rs benchmarks

> **Current numbers live in [`RESULTS.md`](RESULTS.md)** — one ordered file with every benchmark table. This document keeps the narrative it is named for; where a figure here and one there disagree, `RESULTS.md` is authoritative.


64-core AVX-512 server, 376 GB RAM, Ubuntu 24.04. Params throughout are `-k 25 -r 0.01 -t 0.4
-d 0.075 -o 0.3 -m Containment`. Accuracy matches shmap closely (22 918 / 6 902 correct on chrY,
228 165 vs 228 166 on the whole genome) and is unchanged across every thread count below. See
`PROFILING.md` for stage-level detail and `COMPARISON.md` for the mapper-vs-mapper numbers.

The real-WGS sweep below is the current reference. The Table-1 sweep after it is an older
generation kept for continuity — see the note above that section.

Run-to-run variance on a shared machine is a few percent, so cells that appear in more than one
document (here, `PROFILING.md`, `COMPARISON.md`) can differ slightly without either being wrong;
none of these tables is derived from another.

## Thread scaling (`-@`) on real whole-genome data

Measured on a2 with the sharded index build and the two-pass parallel FASTA reader. Output is
**byte-identical across every thread count** on all three datasets, so threading stays
deterministic. `index`/`map` split the wall using the run's own `indexing` timer, so the mapper's
own scaling can be read separately from the index build's.

### 0.9624x, 24 kbp reads — 125 000 reads, 3.00 Gbp

The dataset that was missing. Every other whole-genome set here is 12.8 kbp, and the set named
`allchr_real_24kbp` (below) is neither 24 kbp nor large: its reads are real HiFi at **~13 kb** —
the "24kbp" is the nominal library size — and there are only 2 000 of them, about 0.015x coverage,
~93% of which is indexing. It could never say anything about mapping parallelism. Simulated from `hs1.fa` with
0.5% substitution noise by `profiling/wgs24k/gen24k.py`; valid for throughput and scaling, **not**
for accuracy claims. All 125 000 reads map, 94.0% at Q60.

| Threads | wall | speedup | index | map | map speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 39.43 s | 1.00x | 9.2 s | 30.3 s | 1.00x | 2.18 |
| 2  | 25.65 s | 1.54x | 8.0 s | 17.6 s | 1.72x | 1.78 |
| 4  | 16.22 s | 2.43x | 5.0 s | 11.2 s | 2.70x | 1.84 |
| 8  | 10.02 s | 3.94x | 3.5 s | 6.5 s | 4.67x | 2.02 |
| 16 | 7.38 s | 5.34x | 3.5 s | 3.9 s | 7.83x | 2.05 |
| 32 | 7.62 s | 5.17x | 3.8 s | 3.8 s | **7.98x** | 2.28 |
| 64 | **7.10 s** | **5.55x** | 3.2 s | 3.9 s | 7.69x | 2.36 |

**The mapper scales ~7.9x; the whole run reaches 5.55x, and the difference is the index build.**
By 16 threads mapping is 3.9 s against 3.5 s of indexing — the two are now about equal, where
before the reader work indexing was over half the wall. Anyone asking why whole-genome 24 kbp runs
"don't parallelize well" is looking at the index build, not the mapper and not the read length.

### 0.999x, 12.8 kbp reads — 242 534 real HG002 HiFi reads

| Threads | wall | speedup | index | map | map speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 54.59 s | 1.00x | 9.5 s | 45.1 s | 1.00x | 1.99 |
| 2  | 32.44 s | 1.68x | 7.6 s | 24.8 s | 1.82x | 1.86 |
| 4  | 21.02 s | 2.60x | 5.2 s | 15.8 s | 2.85x | 1.98 |
| 8  | 12.23 s | 4.46x | 3.5 s | 8.7 s | 5.19x | 2.26 |
| 16 | **9.39 s** | **5.81x** | 3.5 s | 5.9 s | 7.60x | 2.93 |
| 32 | 9.47 s | 5.76x | 3.6 s | 5.8 s | **7.73x** | 3.55 |
| 64 | 9.73 s | 5.61x | 3.6 s | 6.2 s | 7.31x | 4.16 |

### 9.987x, 12.8 kbp reads — 2 425 341 real HG002 HiFi reads

| Threads | wall | speedup | index | map | map speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 440.27 s | 1.00x | 9.6 s | 430.6 s | 1.00x | 1.94 |
| 2  | 254.60 s | 1.73x | 7.3 s | 247.3 s | 1.74x | 1.92 |
| 4  | 137.15 s | 3.21x | 4.6 s | 132.6 s | 3.25x | 1.99 |
| 8  | 73.89 s | 5.96x | 3.6 s | 70.3 s | 6.12x | 2.30 |
| 16 | 42.41 s | 10.38x | 2.9 s | 39.5 s | 10.90x | 4.42 |
| 32 | **41.34 s** | **10.65x** | 3.7 s | 37.7 s | **11.43x** | 6.88 |
| 64 | 42.07 s | 10.47x | 3.4 s | 38.7 s | 11.14x | 9.56 |

Deep coverage is where whole-run scaling looks best (10.65x), simply because there is enough
mapping work to bury the fixed index cost — at 10x indexing is 9% of the wall at `-@ 32`, against
50% for the 24 kbp 1x run. **The mapper's own ceiling is a consistent ~7.7-11.4x in all three**,
reached around 16-32 threads.

Peak memory grows with thread count only where the per-worker dense bucket accumulators do
(1.9 → 9.6 GB from `-@ 1` to `-@ 64` at 10x); it is flat to ~8 threads everywhere, and at 1x it
stays near 2 GB.

### What indexing is bounded by now

Three serial floors have been removed in turn, and the numbers below are what is left.

`index_initializing` — one thread doing ~31 M cache-missing hash-map inserts — was flat at ~8.4 s
from 1 thread to 64. Sharding the index by k-mer hash put it at **1.1-1.8 s**. `index_reading` then
became the floor at 4.3-5.6 s, also flat. Parsing over byte ranges helped, but the collector still
concatenated every range into a growing per-segment buffer on one thread — 2.8-3.2 s of a 2.9-3.2 s
reader, dominated not by the memcpy but by doubling reallocations and ~780 k serialised first-touch
page faults. Splitting that into a counting pass and a filling pass, so workers write straight into
disjoint slices of an exactly-sized buffer, put reading at **1.5-1.7 s**. On the 10x run:

| Threads | 1 | 2 | 4 | 8 | 16 | 32 | 64 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `indexing` (wall) | 9.6 | 7.3 | 4.6 | 3.6 | **2.9** | 3.7 | 3.4 |
| `index_reading` | 4.4 | 3.0 | 2.5 | 1.5 | 1.6 | 1.7 | 1.6 |
| ⌐ `fasta_scan` (count) | — | 0.7 | 0.5 | 0.3 | 0.3 | 0.3 | 0.3 |
| ⌐ `fasta_fill` (copy) | — | 3.1 | 2.1 | 1.2 | 1.3 | 1.4 | 1.3 |
| `index_initializing` | 0.0 | 3.1 | 1.6 | 1.8 | 1.1 | 1.7 | 1.6 |
| `index_finalizing` | 0.7 | 0.4 | 0.4 | 0.2 | 0.1 | 0.2 | 0.2 |

(`index_initializing` is 0 and the two `fasta_*` timers are absent at `-@ 1`, where both phases are
folded into their serial paths so they can overlap instead — see `src/index.rs` and `src/io.rs`.)

**Indexing bottoms out near 2.9-3.4 s, down from ~9.4 s.** No single phase dominates any more:
reading and the shard fill are each ~1.5 s, and both are within ~2x of the 0.87 s it takes to
stream the 3.18 GB reference off page cache at all. Further gains here would have to come from
reading less, not from parallelising harder.


## Thread scaling on the Table-1 datasets (older generation)

Kept for continuity. These predate the `refine` memo and the allocation work, and they are **not
reproducible on the current benchmark host**: the `pesho_table1/` scripts survive (under
`minshmap_bench/realworld/`) but its `data/` directory is empty, so the read sets themselves are
gone and these could not be re-measured alongside the numbers above. Treat them as historical.

Output is byte-identical across thread counts; only wall-time/memory vary. Time is the whole run
(shmap indexes and maps in one pass), speedup is vs `-@ 1`, memory is peak RSS:

### chrY

| Threads | 10kbp s | speedup | GB | 24kbp s | speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 36.4 | 1.0x  | 0.13 | 11.4 | 1.0x  | 0.13 |
| 2  | 18.5 | 2.0x  | 0.13 | 6.0  | 1.9x  | 0.13 |
| 4  | 9.4  | 3.9x  | 0.13 | 3.2  | 3.6x  | 0.13 |
| 8  | 5.2  | 7.0x  | 0.13 | 1.9  | 6.0x  | 0.13 |
| 16 | 2.9  | 12.6x | 0.13 | 1.2  | 9.5x  | 0.13 |
| 32 | 1.8  | 20.2x | 0.13 | 0.8  | 14.3x | 0.13 |

### Whole genome

| Threads | 10kbp s | speedup | GB | real ~13kb s | speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 60.6 | 1.0x | 2.42 | 10.9 | 1.0x | 2.47 |
| 2  | 36.7 | 1.7x | 2.05 | 10.7 | 1.0x | 2.01 |
| 4  | 29.1 | 2.1x | 2.05 | 11.1 | 1.0x | 2.07 |
| 8  | 19.4 | 3.1x | 2.08 | 10.3 | 1.1x | 2.05 |
| 16 | 16.1 | 3.8x | 2.52 | 10.8 | 1.0x | 2.05 |
| 32 | 15.1 | 4.0x | 3.12 | 10.4 | 1.0x | 2.04 |

- chrY scales well through 32 threads (up to 20.2x on 10 kbp, 14.3x on 24 kbp) — a small reference
  makes `index_initializing` cheap, so the serial floor that binds the whole-genome runs barely
  exists here.
- Whole-genome 10 kbp plateaus past ~8 threads (memory bandwidth + the serial indexing floor;
  `index_initializing` was single-threaded when these were taken). That floor has since been
  removed by sharding the index — see the sweeps above, where the remaining floor is the FASTA
  reader instead.
- `allchr_real_24kbp` (only 2 000 reads, and **~13 kb** reads despite the name — see above) is ~90%
  indexing, so thread count barely moves it at all — flat ~10-11 s regardless of `-@`. This is the
  dataset the indexing work targets, not the mapping work.
  It is also why that row says nothing about whether long reads parallelize: at ~0.015x coverage
  there is almost no mapping to parallelize, and the reads are not long either. The 125 000-read
  24 kbp set above was built to answer that question properly, and there the mapper scales ~7.9x.
  For *real* long reads see `profiling/real24kbp/` (23.2 kb, 149 438 reads).
- **Memory is flat in thread count except on whole-genome 10 kbp**, where it climbs from 2.05 GB at
  `-@ 4` to 3.12 GB at `-@ 32`. Part of that is the dense bucket accumulator, which is per worker:
  those 10 kbp reads give a half-length of ~127, so the bucket space is ~246 k slots and each
  worker holds ~3.9 MB of it — ~126 MB across 32 workers. That does not account for the full
  ~1 GB and the rest has not been attributed; it is bounded by `MAX_DENSE_SLOTS` (~32 MB/worker)
  regardless. Note the long-read WGS runs in `COMPARISON.md` move the opposite way — ONT peak RSS
  at `-@ 4` drops 22.5 GB -> 9.1 GB, because there it was the *old* accumulator that scaled with
  thread count.
