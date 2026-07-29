# shmap-rs benchmarks

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

Measured on a2 at `9400936`, i.e. **with the sharded index build**. Output is
**byte-identical across every thread count** on all three datasets, so threading stays
deterministic. `index`/`map` split the wall using the run's own `indexing` timer, so the mapper's
own scaling can be read separately from the index build's.

### 0.9624x, 24 kbp reads — 125 000 reads, 3.00 Gbp

The dataset that was missing. Every other whole-genome set here is 12.8 kbp, and the only 24 kbp
WGS set (`allchr_real_24kbp`, below) is 2 000 reads — about 0.015x coverage, ~93% of which is
indexing, so it could never say anything about mapping parallelism. Simulated from `hs1.fa` with
0.5% substitution noise by `profiling/wgs24k/gen24k.py`; valid for throughput and scaling, **not**
for accuracy claims. All 125 000 reads map, 94.0% at Q60, 207 µs/read.

| Threads | wall | speedup | index | map | map speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 38.55 s | 1.00x | 9.3 s | 29.3 s | 1.00x | 2.05 |
| 2  | 23.38 s | 1.65x | 7.8 s | 15.6 s | 1.87x | 1.72 |
| 4  | 18.98 s | 2.03x | 7.9 s | 11.1 s | 2.64x | 1.73 |
| 8  | 12.22 s | 3.15x | 6.0 s | 6.2 s | 4.68x | 1.77 |
| 16 | **9.98 s** | **3.86x** | 6.4 s | 3.6 s | **8.07x** | 1.82 |
| 32 | 10.07 s | 3.83x | 6.0 s | 4.1 s | 7.19x | 1.94 |
| 64 | 9.88 s | 3.90x | 5.9 s | 4.0 s | 7.41x | 2.11 |

**The mapper scales 8.07x; the whole run only reaches 3.90x, and the entire difference is the
index build.** By 16 threads mapping is down to 3.6 s while indexing is 6.4 s — 64% of the wall —
and indexing does not improve past that no matter how many cores are added. Anyone asking why
whole-genome 24 kbp runs "don't parallelize well" is looking at this: it is not the mapper.

### 0.999x, 12.8 kbp reads — 242 534 real HG002 HiFi reads

| Threads | wall | speedup | index | map | map speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 53.97 s | 1.00x | 9.2 s | 44.8 s | 1.00x | 2.14 |
| 2  | 32.01 s | 1.69x | 8.9 s | 23.1 s | 1.94x | 1.83 |
| 4  | 22.01 s | 2.45x | 6.8 s | 15.2 s | 2.95x | 1.90 |
| 8  | 14.65 s | 3.68x | 6.1 s | 8.6 s | 5.24x | 1.97 |
| 16 | 12.28 s | 4.39x | 6.6 s | 5.7 s | 7.90x | 2.43 |
| 32 | **12.10 s** | **4.46x** | 6.0 s | 6.1 s | 7.36x | 3.22 |
| 64 | 13.22 s | 4.08x | 7.1 s | 6.1 s | 7.32x | 3.91 |

### 9.987x, 12.8 kbp reads — 2 425 341 real HG002 HiFi reads

| Threads | wall | speedup | index | map | map speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 439.27 s | 1.00x | 10.0 s | 429.2 s | 1.00x | 1.84 |
| 2  | 261.64 s | 1.68x | 8.0 s | 253.7 s | 1.69x | 1.87 |
| 4  | 146.56 s | 3.00x | 7.6 s | 138.9 s | 3.09x | 1.90 |
| 8  | 76.71 s | 5.73x | 6.3 s | 70.4 s | 6.10x | 2.10 |
| 16 | 44.50 s | 9.87x | 5.6 s | 38.9 s | 11.04x | 4.06 |
| 32 | **42.64 s** | **10.30x** | 6.1 s | 36.5 s | **11.75x** | 6.20 |
| 64 | 47.83 s | 9.18x | 6.4 s | 41.4 s | 10.37x | 8.76 |

Deep coverage is where whole-run scaling looks best (10.30x), simply because there is enough
mapping work to bury the fixed index cost — at 10x indexing is 14% of the wall at `-@ 32`, against
64% for the 24 kbp 1x run. **The mapper's own ceiling is the same ~7-12x in all three**, reached
around 16-32 threads.

### Indexing is now bounded by the FASTA reader, not the inserts

`index_initializing` used to be the serial floor — one thread doing ~31 M cache-missing hash-map
inserts, flat at ~8.4 s from 1 thread to 64. Sharding the index by k-mer hash fixed that: it is
**1.1-1.8 s** everywhere above one thread (and 0 at `-@ 1`, where it is folded into the collector).

What is left does not scale at all:

| Threads (10x run) | 1 | 2 | 4 | 8 | 16 | 32 | 64 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `indexing` (wall) | 10.0 | 8.0 | 7.6 | 6.3 | 5.6 | 6.1 | 6.4 |
| `index_reading` (serial) | 4.4 | 4.4 | 5.5 | 4.4 | 4.3 | 4.5 | 4.6 |
| `index_initializing` | 0.0 | 3.1 | 2.0 | 1.8 | 1.1 | 1.4 | 1.6 |

**`index_reading` is 4.3-5.6 s regardless of `-@`, and is now ~70-80% of all indexing.** One
thread parses the whole 3.18 GB FASTA. Indexing bottoms out near 6 s and cannot go below it at any
core count, which is what caps every whole-run speedup in the tables above. Parallel parsing over
byte ranges — FASTA records are independently locatable — is the next thing that would move these
numbers.


## Thread scaling on the Table-1 datasets (older generation)

Kept for continuity. These predate the `refine` memo and the allocation work, and they are **not
reproducible on the current benchmark host** — `pesho_table1/` is no longer present there, so they
could not be re-measured alongside the numbers above. Treat them as historical.

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

| Threads | 10kbp s | speedup | GB | real 24kbp s | speedup | GB |
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
- `real_24kbp` (only 2 000 reads) is ~90% indexing, so thread count barely moves it at all — flat
  ~10-11 s regardless of `-@`. This is the dataset the indexing work targets, not the mapping work.
  It is also why that row says nothing about whether 24 kbp reads parallelize: at ~0.015x coverage
  there is almost no mapping to parallelize. The 125 000-read 24 kbp set above was built to answer
  that question properly, and there the mapper scales 8.07x.
- **Memory is flat in thread count except on whole-genome 10 kbp**, where it climbs from 2.05 GB at
  `-@ 4` to 3.12 GB at `-@ 32`. Part of that is the dense bucket accumulator, which is per worker:
  those 10 kbp reads give a half-length of ~127, so the bucket space is ~246 k slots and each
  worker holds ~3.9 MB of it — ~126 MB across 32 workers. That does not account for the full
  ~1 GB and the rest has not been attributed; it is bounded by `MAX_DENSE_SLOTS` (~32 MB/worker)
  regardless. Note the long-read WGS runs in `COMPARISON.md` move the opposite way — ONT peak RSS
  at `-@ 4` drops 22.5 GB -> 9.1 GB, because there it was the *old* accumulator that scaled with
  thread count.
