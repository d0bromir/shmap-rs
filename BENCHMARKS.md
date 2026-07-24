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
| 1  | 91.5 | 1.0x | 0.19 | 23.5 | 1.0x | 0.19 |
| 2  | 47.7 | 1.9x | 0.19 | 12.2 | 1.9x | 0.19 |
| 4  | 23.7 | 3.9x | 0.19 | 6.5  | 3.6x | 0.19 |
| 8  | 12.4 | 7.4x | 0.19 | 3.7  | 6.4x | 0.19 |
| 16 | 6.9  | 13.3x | 0.19 | 2.4  | 9.8x | 0.19 |
| 32 | 4.2  | 21.8x | 0.19 | 1.7  | 13.8x | 0.19 |

### Whole genome

| Threads | 10kbp s | speedup | GB | real 24kbp s | speedup | GB |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 83.1 | 1.0x | 2.73 | 11.6 | 1.0x | 2.73 |
| 2  | 46.4 | 1.8x | 2.43 | 10.5 | 1.1x | 2.40 |
| 4  | 32.5 | 2.6x | 2.16 | 10.8 | 1.1x | 2.02 |
| 8  | 21.9 | 3.8x | 2.34 | 12.0 | 1.0x | 2.02 |
| 16 | 18.5 | 4.5x | 2.04 | 10.6 | 1.1x | 2.02 |
| 32 | 15.6 | 5.3x | 2.55 | 12.8 | 0.9x | 2.02 |

- chrY scales well through 32 threads (up to 22x).
- Whole-genome plateaus past ~8 threads (memory bandwidth + indexing floor).
- `real_24kbp` (only 2 000 reads) is indexing-dominated, so thread count barely matters — flat
  ~10-13s regardless of `-@`.
- Peak memory is flat (~2-2.7 GB) regardless of thread count.
