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

The current reference sweep: real HG002 HiFi against the whole T2T-CHM13 genome at `f85d9a2`,
measured on a2 (`profiling/full_suite_a2/`). Output is **byte-identical across every thread count**
at both depths, so threading stays deterministic. `index`/`map` split the wall using the run's own
`indexing` timer.

### 0.999x — 242 534 reads

| Threads | wall | speedup | index | map | map speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 53.69 s | 1.00x | 9.3 s | 44.4 s | 1.00x | 1.96 |
| 2  | 33.07 s | 1.62x | 10.0 s | 23.1 s | 1.92x | 2.07 |
| 4  | 24.74 s | 2.17x | 9.3 s | 15.5 s | 2.87x | 2.03 |
| 8  | 19.13 s | 2.81x | 10.2 s | 9.0 s | 4.94x | 2.07 |
| 16 | 15.11 s | 3.55x | 9.2 s | 5.9 s | 7.53x | 2.56 |
| 32 | 16.59 s | 3.24x | 10.4 s | 6.1 s | 7.22x | 3.16 |
| 64 | 15.54 s | 3.45x | 9.4 s | 6.1 s | 7.23x | 3.80 |

### 9.987x — 2 425 341 reads

| Threads | wall | speedup | index | map | map speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 434.36 s | 1.00x | 11.7 s | 422.7 s | 1.00x | 1.96 |
| 2  | 228.08 s | 1.90x | 9.2 s | 218.9 s | 1.93x | 2.05 |
| 4  | 140.22 s | 3.10x | 9.2 s | 131.0 s | 3.23x | 2.03 |
| 8  | 78.95 s | 5.50x | 10.3 s | 68.7 s | 6.15x | 2.13 |
| 16 | 49.18 s | 8.83x | 10.0 s | 39.1 s | 10.80x | 4.16 |
| 32 | **47.17 s** | **9.21x** | 9.5 s | 37.6 s | **11.23x** | 7.30 |
| 64 | 48.40 s | 8.97x | 9.4 s | 39.0 s | 10.84x | 7.32 |

**Indexing does not scale at all** — it sits at 9.2-11.7 s from 1 thread to 64. Splitting it by
sub-stage on the 10x run shows exactly why:

| Threads | 1 | 2 | 4 | 8 | 16 | 32 | 64 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `indexing` (wall) | 11.7 | 9.2 | 9.2 | 10.3 | 10.0 | 9.5 | 9.4 |
| `index_initializing` (serial) | 10.0 | 7.3 | 7.5 | 8.4 | 8.3 | 7.7 | 7.7 |
| `index_sketching` (CPU, all workers) | 6.3 | 6.0 | 6.2 | 8.4 | 13.7 | 19.3 | 21.1 |

Sketching parallelizes fine — its *wall* share collapses, and the growing CPU total is the usual
cost of spreading it over more workers. `index_initializing` is the floor: it is one thread doing
~31 M hash-map inserts into a table far larger than cache, it is unaffected by `-@`, and at
`-@ 32` it is **~80% of all indexing time**. Sharding `h2single`/`h2multi` by hash across workers
(see `PROFILING.md`'s remaining-bottlenecks section) is the change that would move this, and these
numbers are the case for doing it.

Mapping itself plateaus at **~11x around 16-32 threads**. Peak memory is flat to 8 threads and then
climbs (2.13 → 7.30 GB from 8 to 32 at 10x) as per-worker dense accumulators multiply; it stops
growing past 32 because that is where mapping stops getting faster too.

Taken together the two ceilings cap a whole run at ~9.2x on 64 cores, and **which ceiling binds
depends on depth**: at 1x, indexing is already 60% of the wall by 16 threads, so the run tops out
at 3.5x; at 10x there is enough mapping work to reach 9.2x.

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
  `index_initializing` is single-threaded — see `PROFILING.md`). The real-WGS sweep above measures
  that floor directly: ~7.7 s serial, ~80% of all indexing time at `-@ 32`.
- `real_24kbp` (only 2 000 reads) is ~90% indexing, so thread count barely moves it at all — flat
  ~10-11 s regardless of `-@`. This is the dataset the indexing work targets, not the mapping work.
- **Memory is flat in thread count except on whole-genome 10 kbp**, where it climbs from 2.05 GB at
  `-@ 4` to 3.12 GB at `-@ 32`. Part of that is the dense bucket accumulator, which is per worker:
  those 10 kbp reads give a half-length of ~127, so the bucket space is ~246 k slots and each
  worker holds ~3.9 MB of it — ~126 MB across 32 workers. That does not account for the full
  ~1 GB and the rest has not been attributed; it is bounded by `MAX_DENSE_SLOTS` (~32 MB/worker)
  regardless. Note the long-read WGS runs in `COMPARISON.md` move the opposite way — ONT peak RSS
  at `-@ 4` drops 22.5 GB -> 9.1 GB, because there it was the *old* accumulator that scaled with
  thread count.
