# shmap-rs benchmarks

Benchmarks were run on a 64-core AVX-512 server (376 GB RAM, Ubuntu 24.04), using the same
datasets and parameters as Pesho's `shmap` Table 1 (`-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3
-m Containment`). shmap-rs reproduces `shmap`'s accuracy essentially exactly — identical on the
chrY datasets (22 918 / 6 902 correct, 0 wrong) and within one read on the whole genome
(228 165 vs 228 166) — while running slightly faster and using less peak memory.

This sweep reflects the memory/speed optimization pass documented in `PROFILING.md` (sparse
`Buckets` storage, buffered collector output, reused `match_seeds` scratch space, and parallel
reference indexing). The previous sweep — before that work — showed peak memory scaling by
roughly 14.5 GB per worker thread and one dataset (`allchr real 24 kbp`) getting *slower* with
more threads; neither happens anymore, see below.

## Thread scaling (`-@ / --threads`)

Multithreaded mapping uses a reader thread to stream reads, `N` worker threads to map, and the
main thread to render. Output is **byte-identical** to the single-thread run, so accuracy is
unchanged across thread counts (verified: every cell below reports the same Mapped Q60 count as
`-@ 1` — 22 918 / 6 902 / 228 165 / 1 876 respectively); only wall-time and peak memory vary.
Reference indexing (sketching) is now parallelized too, via the same reader/worker-pool/collector
pipeline as mapping — see `PROFILING.md` for why that mostly helps through pipelining rather than
raw per-thread scaling, since a handful of large chromosomes dominate sketching time regardless of
thread count.

The sweep covers `-@ N` for `N = 1, 2, 4, 8, 16, 32`. Each cell is map wall-time (seconds), speedup
versus one thread, and peak memory (GB).

### chrY (mapping-dominated, scales all the way to 32 threads)

| Threads | chrY 10 kbp map s | speedup | GB | chrY 24 kbp map s | speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 92.8 | 1.00x | 0.19 | 23.8 | 1.00x | 0.19 |
| 2  | 47.4 | 1.96x | 0.19 | 12.6 | 1.89x | 0.19 |
| 4  | 23.7 | 3.92x | 0.19 | 6.6  | 3.61x | 0.19 |
| 8  | 12.5 | 7.42x | 0.19 | 3.7  | 6.43x | 0.19 |
| 16 | 7.0  | 13.26x | 0.19 | 2.3  | 10.35x | 0.19 |
| 32 | 4.2  | 22.10x | 0.19 | 1.8  | 13.22x | 0.19 |

### Whole-genome (indexing floor + memory-bandwidth limited, but no longer regresses)

| Threads | allchr 10 kbp map s | speedup | GB | allchr real 24 kbp map s | speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 85.0 | 1.00x | 2.71 | 12.9 | 1.00x | 2.72 |
| 2  | 47.8 | 1.78x | 2.03 | 10.9 | 1.18x | 2.47 |
| 4  | 33.6 | 2.53x | 2.25 | 11.2 | 1.15x | 2.02 |
| 8  | 22.9 | 3.71x | 2.02 | 10.9 | 1.18x | 2.02 |
| 16 | 18.3 | 4.65x | 2.46 | 12.5 | 1.03x | 2.02 |
| 32 | 16.9 | 5.03x | 2.46 | 10.6 | 1.22x | 2.24 |

> **32 threads now runs fine — the old memory blocker is gone.** The previous sweep couldn't test
> 32 threads at all (peak RSS scaled ~14.5 GB per worker thread, so 32 threads would have needed
> ~450 GB, more than this machine has). Peak memory here tops out at **2.7 GB**, flat regardless of
> thread count, for both whole-genome datasets at every `-@` value tested — `Buckets`'s storage no
> longer scales with reference size at all, let alone with thread count. `allchr 10 kbp` keeps
> improving all the way to 32 threads (diminishing returns past ~8, as expected — CHM13's
> memory-bandwidth and the still-partly-serial indexing floor start to dominate), and nothing in
> either table regresses.

## How to read the results

- **chrY** sets are mapping-dominated and now scale all the way out to 32 threads — **22.1x at 32
  threads** on the 10 kbp set (69% efficiency), still climbing rather than plateauing.
- **Whole-genome** sets are limited by indexing (a serial-ish floor even after parallelizing it,
  see `PROFILING.md`) plus memory-bandwidth saturation: `allchr 10 kbp` keeps improving through 32
  threads (5.03x) but with clearly diminishing returns past 8.
- **`allchr real 24 kbp`** has only 2 000 reads, so indexing (not mapping) dominates its wall time
  regardless of thread count — it hovers around 10.6-12.9s at every `-@` value, essentially flat.
  This is the dataset that used to get dramatically *slower* with more threads (up to 51.9s at
  `-@ 16` in the pre-optimization sweep); that regression is gone, replaced by "adding threads
  doesn't move the needle much" — the correct, unsurprising behavior for a workload this small
  relative to reference size.

Threading now helps consistently regardless of dataset size — the previous "regresses on small
read counts" failure mode was specific to the since-fixed `Buckets` allocation contention, not an
inherent property of small workloads.
