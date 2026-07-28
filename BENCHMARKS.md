# shmap-rs benchmarks

64-core AVX-512 server, 376 GB RAM, Ubuntu 24.04. Same datasets/params as Pesho's `shmap` Table 1
(`-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment`). Accuracy matches shmap closely (22 918 /
6 902 correct on chrY, 228 165 vs 228 166 on the whole genome) and is unchanged across every
thread count below. See `PROFILING.md` for stage-level detail and `COMPARISON.md` for the
mapper-vs-mapper numbers.

Measured with the dense bucket accumulator in place (`profiling/benchmark.py --datasets all
--threads N --only shmap-rs`), one thread count after another on an idle host. The previous
generation of these numbers is in git history; it predates that work and is not comparable.

This is a **separate sweep** from the reports in `profiling/`, so the `-@ 1` and `-@ 16` rows here
differ from the same cells in `PROFILING.md` / `COMPARISON.md` by a few percent (e.g. chrY 10 kbp
36.4 s here vs 35.9 s there). That is run-to-run variance on a shared machine, not a discrepancy —
neither table is derived from the other.

## Thread scaling (`-@`)

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

- chrY scales well through 32 threads (up to 20.2x on 10 kbp, 14.3x on 24 kbp).
- Whole-genome 10 kbp plateaus past ~8 threads (memory bandwidth + the serial indexing floor;
  `index_initializing` is single-threaded — see `PROFILING.md`).
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
