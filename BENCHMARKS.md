# shmap-rs benchmarks

> **Note:** the thread-scaling numbers below (including the memory-per-worker caveat) predate a
> memory/speed optimization pass documented in `PROFILING.md` (`Buckets`'s primary storage moved
> from a whole-reference-sized array to a sparse map). Peak RSS on whole-genome runs is now flat
> at ~2.3 GB regardless of thread count instead of scaling per worker, and the "gets slower with
> more threads" case below no longer reproduces — see `PROFILING.md`'s "Optimizations applied"
> section for current, re-measured numbers. This table hasn't been re-run at the full 1/2/4/8/16
> thread sweep since; treat it as historical unless/until it is.

Benchmarks were run on a 64-core AVX-512 server (376 GB RAM, Ubuntu 24.04), using the same
datasets and parameters as Pesho's `shmap` Table 1 (`-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3
-m Containment`). shmap-rs reproduces `shmap`'s accuracy essentially exactly — identical on the
chrY datasets (22 918 / 6 902 correct, 0 wrong) and within one read on the whole genome
(228 165 vs 228 166) — while running slightly faster and using less peak memory.

## Thread scaling (`-@ / --threads`)

Multithreaded mapping uses a reader thread to stream reads, `N` worker threads to map, and the
main thread to render. Output is **byte-identical** to the single-thread run, so accuracy is
unchanged across thread counts; only wall-time and peak memory vary. Sketching the reference is
single-threaded, so it acts as a fixed serial floor (Amdahl's law) on the whole-genome sets.

The sweep covers `-@ N` for `N = 1, 2, 4, 8, 16`. Each cell is map wall-time (seconds), speedup
versus one thread, and peak memory (GB).

### chrY (mapping-dominated, near-linear scaling)

| Threads | chrY 10 kbp map s | speedup | GB | chrY 24 kbp map s | speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 94.2 | 1.00x | 0.31 | 24.7 | 1.00x | 0.31 |
| 2  | 47.8 | 1.97x | 0.59 | 12.6 | 1.96x | 0.59 |
| 4  | 24.6 | 3.83x | 1.16 | 7.4 | 3.34x | 1.15 |
| 8  | 13.0 | 7.25x | 2.28 | 4.0 | 6.18x | 2.27 |
| 16 | 7.3 | 12.9x | 4.53 | 2.5 | 9.88x | 4.52 |

### Whole-genome (serial-sketch + memory-bandwidth limited)

| Threads | allchr 10 kbp map s | speedup | GB | allchr real 24 kbp map s | speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 120.4 | 1.00x | 15.72 | 32.1 | 1.00x | 15.72 |
| 2  | 72.9 | 1.65x | 29.66 | 34.8 | 0.92x | 29.66 |
| 4  | 56.5 | 2.13x | 57.54 | 35.3 | 0.91x | 57.10 |
| 8  | 51.8 | 2.32x | 113.30 | 44.1 | 0.73x | 112.43 |
| 16 | 58.1 | 2.07x | 225.05 | 51.9 | 0.62x | 220.92 |

> **32 threads was not benchmarked — it requires too much memory.** Peak RSS on the whole-genome
> datasets grows by roughly 14.5 GB per worker thread, so 32 threads would need on the order of
> **~450 GB**, exceeding the 376 GB test machine. In practice the whole-genome run at 32 threads
> pinned all 376 GB of RAM and spilled into swap, thrashing (CPU utilisation collapsed from
> ~1570% to ~250%) with no wall-time benefit, so the sweep is capped at 16 threads.

## How to read the results

- **chrY** sets are mapping-dominated and scale near-linearly — up to **12.9x on 16 threads**
  (81% efficiency).
- **Whole-genome** sets are limited by the single-threaded reference sketch (a fixed ~25 s floor)
  plus memory-bandwidth saturation: they plateau around 8 threads and then regress (`allchr 10 kbp`
  is best at 8 threads, 51.8 s, and slower at 16).
- **`allchr real 24 kbp`** has only 2 000 reads, so it is index/sketch-bound and actually gets
  *slower* with more threads (per-thread overhead outweighs the tiny mapping work).

Threading helps most when there are many reads to map against a reference small enough to fit
comfortably in memory at the chosen thread count.
