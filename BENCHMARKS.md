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

Measured on a2 at `1f1f521` — with the sharded index build **and** the parallel FASTA reader.
Output is **byte-identical across every thread count** on all three datasets, so threading stays
deterministic. `index`/`map` split the wall using the run's own `indexing` timer, so the mapper's
own scaling can be read separately from the index build's.

### 0.9624x, 24 kbp reads — 125 000 reads, 3.00 Gbp

The dataset that was missing. Every other whole-genome set here is 12.8 kbp, and the only 24 kbp
WGS set (`allchr_real_24kbp`, below) is 2 000 reads — about 0.015x coverage, ~93% of which is
indexing, so it could never say anything about mapping parallelism. Simulated from `hs1.fa` with
0.5% substitution noise by `profiling/wgs24k/gen24k.py`; valid for throughput and scaling, **not**
for accuracy claims. All 125 000 reads map, 94.0% at Q60.

| Threads | wall | speedup | index | map | map speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 39.01 s | 1.00x | 9.2 s | 29.8 s | 1.00x | 2.09 |
| 2  | 24.43 s | 1.60x | 6.9 s | 17.6 s | 1.69x | 1.97 |
| 4  | 15.65 s | 2.49x | 5.3 s | 10.4 s | 2.87x | 2.02 |
| 8  | 12.16 s | 3.21x | 6.1 s | 6.1 s | 4.90x | 2.34 |
| 16 | 8.70 s | 4.48x | 4.7 s | 4.0 s | **7.53x** | 2.31 |
| 32 | **8.38 s** | **4.66x** | 4.4 s | 4.0 s | 7.44x | 2.58 |
| 64 | 8.93 s | 4.37x | 4.8 s | 4.1 s | 7.28x | 2.74 |

**The mapper scales 7.53x; the whole run reaches 4.66x, and the difference is the index build.**
By 16 threads mapping is down to 4.0 s against 4.7 s of indexing, so indexing is now over half the
wall. Anyone asking why whole-genome 24 kbp runs "don't parallelize well" is looking at this: it
is not the mapper, and it is not the read length.

### 0.999x, 12.8 kbp reads — 242 534 real HG002 HiFi reads

| Threads | wall | speedup | index | map | map speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 54.19 s | 1.00x | 9.6 s | 44.6 s | 1.00x | 1.91 |
| 2  | 30.63 s | 1.77x | 7.1 s | 23.5 s | 1.90x | 1.97 |
| 4  | 20.22 s | 2.68x | 5.1 s | 15.1 s | 2.96x | 2.15 |
| 8  | 13.88 s | 3.90x | 5.2 s | 8.7 s | 5.11x | 2.45 |
| 16 | **9.95 s** | **5.45x** | 4.2 s | 5.8 s | **7.70x** | 3.15 |
| 32 | 11.14 s | 4.86x | 5.2 s | 6.0 s | 7.49x | 3.83 |
| 64 | 11.42 s | 4.75x | 5.3 s | 6.2 s | 7.24x | 4.57 |

### 9.987x, 12.8 kbp reads — 2 425 341 real HG002 HiFi reads

| Threads | wall | speedup | index | map | map speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 431.66 s | 1.00x | 9.2 s | 422.4 s | 1.00x | 2.06 |
| 2  | 243.94 s | 1.77x | 8.5 s | 235.5 s | 1.79x | 2.10 |
| 4  | 143.20 s | 3.01x | 6.6 s | 136.6 s | 3.09x | 2.25 |
| 8  | 75.84 s | 5.69x | 6.0 s | 69.9 s | 6.05x | 2.60 |
| 16 | 43.63 s | 9.89x | 4.6 s | 39.0 s | 10.83x | 4.77 |
| 32 | **41.80 s** | **10.33x** | 5.2 s | 36.6 s | **11.55x** | 6.68 |
| 64 | 43.87 s | 9.84x | 5.5 s | 38.4 s | 11.00x | 7.87 |

Deep coverage is where whole-run scaling looks best (10.33x), simply because there is enough
mapping work to bury the fixed index cost — at 10x indexing is 12% of the wall at `-@ 32`, against
53% for the 24 kbp 1x run. **The mapper's own ceiling is a consistent ~7-12x in all three**,
reached around 16-32 threads.

Peak memory grows with thread count only where the per-worker dense bucket accumulators do
(1.9 → 4.6 GB from `-@ 1` to `-@ 64` at 1x, 2.1 → 7.9 GB at 10x); it is flat to ~8 threads
everywhere.

### What indexing is bounded by now

Two serial floors have been removed in turn, and the numbers below are what is left.

`index_initializing` — one thread doing ~31 M cache-missing hash-map inserts — used to be flat at
~8.4 s from 1 thread to 64. Sharding the index by k-mer hash put it at **1.5-1.9 s**. `index_reading`
then became the floor at 4.3-5.6 s, also flat; parsing the FASTA over byte ranges put it at
**2.9-3.4 s**. On the 10x run:

| Threads | 1 | 2 | 4 | 8 | 16 | 32 | 64 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `indexing` (wall) | 9.2 | 8.5 | 6.6 | 6.0 | **4.6** | 5.2 | 5.5 |
| `index_reading` | 4.4 | 2.9 | 4.1 | 4.3 | 2.9 | 3.2 | 3.4 |
| `index_initializing` | 0.0 | 4.5 | 2.3 | 1.5 | 1.5 | 1.9 | 1.8 |
| `index_finalizing` | 0.7 | 0.7 | 0.2 | 0.2 | 0.2 | 0.2 | 0.1 |

(`index_initializing` is 0 at `-@ 1`, where the shard fill is folded into the collector so it can
overlap reading and sketching instead — see `src/index.rs`.)

Indexing now bottoms out near **4.6 s**, down from ~9.4 s. Reading is still the largest single
piece and has not gone as far as the worker count should allow: ~8 readers over ~3.5 s of parse
CPU plus a 0.87 s I/O floor predicts ~1.3 s. The gap is the collector, which still concatenates
every byte range into per-segment buffers on one thread. Giving workers a counting pass so they
can write straight into a preallocated segment buffer is the next thing that would move these
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
  that question properly, and there the mapper scales 7.53x.
- **Memory is flat in thread count except on whole-genome 10 kbp**, where it climbs from 2.05 GB at
  `-@ 4` to 3.12 GB at `-@ 32`. Part of that is the dense bucket accumulator, which is per worker:
  those 10 kbp reads give a half-length of ~127, so the bucket space is ~246 k slots and each
  worker holds ~3.9 MB of it — ~126 MB across 32 workers. That does not account for the full
  ~1 GB and the rest has not been attributed; it is bounded by `MAX_DENSE_SLOTS` (~32 MB/worker)
  regardless. Note the long-read WGS runs in `COMPARISON.md` move the opposite way — ONT peak RSS
  at `-@ 4` drops 22.5 GB -> 9.1 GB, because there it was the *old* accumulator that scaled with
  thread count.
