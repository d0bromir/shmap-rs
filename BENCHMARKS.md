# shmap-rs benchmarks

64-core AVX-512 server, 376 GB RAM, Ubuntu 24.04. Same datasets/params as Pesho's `shmap` Table 1
(`-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment`). Accuracy matches shmap closely (22 918 /
6 902 correct on chrY, 228 165 vs 228 166 on the whole genome). See `PROFILING.md` for the
memory/speed optimization pass this sweep reflects.

## Thread scaling (`-@`)

Output is byte-identical across thread counts (verified: same Mapped Q60 at every `-@`); only
wall-time/memory vary. Map wall-time (s), speedup vs 1 thread, peak memory (GB):

### chrY

| Threads | 10kbp s | speedup | GB | 24kbp s | speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 92.8 | 1.0x | 0.19 | 23.8 | 1.0x | 0.19 |
| 2  | 47.4 | 2.0x | 0.19 | 12.6 | 1.9x | 0.19 |
| 4  | 23.7 | 3.9x | 0.19 | 6.6  | 3.6x | 0.19 |
| 8  | 12.5 | 7.4x | 0.19 | 3.7  | 6.4x | 0.19 |
| 16 | 7.0  | 13.3x | 0.19 | 2.3  | 10.3x | 0.19 |
| 32 | 4.2  | 22.1x | 0.19 | 1.8  | 13.2x | 0.19 |

### Whole genome

| Threads | 10kbp s | speedup | GB | real 24kbp s | speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 85.0 | 1.0x | 2.71 | 12.9 | 1.0x | 2.72 |
| 2  | 47.8 | 1.8x | 2.03 | 10.9 | 1.2x | 2.47 |
| 4  | 33.6 | 2.5x | 2.25 | 11.2 | 1.2x | 2.02 |
| 8  | 22.9 | 3.7x | 2.02 | 10.9 | 1.2x | 2.02 |
| 16 | 18.3 | 4.6x | 2.46 | 12.5 | 1.0x | 2.02 |
| 32 | 16.9 | 5.0x | 2.46 | 10.6 | 1.2x | 2.24 |

- chrY scales well through 32 threads (up to 22x).
- Whole-genome plateaus past ~8 threads (memory bandwidth + indexing floor).
- `real_24kbp` (only 2 000 reads) is indexing-dominated, so thread count barely matters — flat
  ~11s regardless of `-@`. This used to get *slower* with more threads (51.9s at `-@ 16`); that
  regression is fixed, see `PROFILING.md`.
- Peak memory is flat regardless of thread count. Before the fix it scaled ~14.5 GB/worker, which
  is why 32 threads was untestable (would've needed ~450 GB).
