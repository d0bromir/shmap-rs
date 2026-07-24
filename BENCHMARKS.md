# shmap-rs benchmarks

64-core AVX-512 server, 376 GB RAM, Ubuntu 24.04. Same datasets/params as Pesho's `shmap` Table 1
(`-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment`). Accuracy matches shmap closely (22 918 /
6 902 correct on chrY, 228 165 vs 228 166 on the whole genome) and is unchanged across every
thread count below. See `PROFILING.md` for stage-level detail.

## Thread scaling (`-@`)

Output is byte-identical across thread counts; only wall-time/memory vary. Map wall-time (s),
speedup vs 1 thread, peak memory (GB):

### chrY

| Threads | 10kbp s | speedup | GB | 24kbp s | speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 73.2 | 1.0x | 0.19 | 19.0 | 1.0x | 0.19 |
| 2  | 38.5 | 1.9x | 0.19 | 10.0 | 1.9x | 0.19 |
| 4  | 19.4 | 3.8x | 0.19 | 5.5  | 3.5x | 0.19 |
| 8  | 10.2 | 7.2x | 0.19 | 3.1  | 6.1x | 0.19 |
| 16 | 5.7  | 12.8x | 0.19 | 2.1  | 9.0x | 0.19 |
| 32 | 3.4  | 21.5x | 0.19 | 1.5  | 12.7x | 0.19 |

### Whole genome

| Threads | 10kbp s | speedup | GB | real 24kbp s | speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 82.4 | 1.0x | 2.73 | 11.5 | 1.0x | 2.73 |
| 2  | 46.1 | 1.8x | 2.18 | 10.5 | 1.1x | 2.43 |
| 4  | 35.1 | 2.3x | 2.02 | 10.3 | 1.1x | 2.45 |
| 8  | 22.8 | 3.6x | 2.42 | 10.6 | 1.1x | 2.25 |
| 16 | 17.0 | 4.8x | 2.49 | 12.3 | 0.9x | 2.02 |
| 32 | 16.5 | 5.0x | 2.47 | 10.3 | 1.1x | 2.50 |

- chrY scales well through 32 threads (up to 21.5x).
- Whole-genome plateaus past ~8 threads (memory bandwidth + indexing floor).
- `real_24kbp` (only 2 000 reads) is indexing-dominated, so thread count barely matters — flat
  ~10-12s regardless of `-@`.
- Peak memory is flat (~2-2.7 GB) regardless of thread count.
