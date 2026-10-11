# Flame graphs, 2.0.0, a2 (x86_64)

Where time goes in the original mapper, per suite benchmark. **A diagnostic, not a
measurement**: use it to find candidates, then judge changes with the suite
([why](../../../profiling/README.md#instrumenting-a-run)).

| | |
|---|---|
| commit | `19e55af711fa599e79fb07d98b099be8a1b8a030` (tag `2.0.0`) |
| host | `a2`, 64-core Xeon Gold 5218, idle (load 0.0-1.7), bench lock held |
| params | suite `paper` (`-k25 -r0.01`) for B01-B05, `short-read` (`-r0.1`) for B06-B09; Containment |
| threads | `-@1` for B01-B05; `-@32` for B06-B09 (the suite's setting; `-@1` is hours) |
| sampling | `perf record -F 99 --call-graph fp`, cycles, user + kernel |
| build | release, `-C force-frame-pointers=yes`, debug info; same LTO profile otherwise |
| driver | [`profiling/flamegraph_suite.sh`](../../../profiling/flamegraph_suite.sh), summary by [`profiling/flamegraph_summary.py`](../../../profiling/flamegraph_summary.py) |

**galaxy (aarch64) was not profiled**: `kernel.perf_event_paranoid=4` there and
unprivileged `perf` is refused. Rerun the same script once it is lowered.

## Files

`B0N.svg` (open in a browser; search and zoom work), `B0N.top.txt` (`perf report` by symbol),
`B0N.time` (`/usr/bin/time -v`), [`top-frames.md`](top-frames.md) (self time per function, all
benchmarks), `summary.tsv`, `RUN.txt`. The raw `perf.data` and per-run stderr (up to 111 MB)
are not kept.

## Wall time under perf

| bench | reads | threads | wall s | peak RSS GB |
|---|---|---:|---:|---:|
| B01 | HiFi 23 kb 1.1x | 1 | 36.5 | 2.7 |
| B02 | simulated 24 kb | 1 | 30.0 | 2.8 |
| B03 | HiFi 12.8 kb 1x | 1 | 41.6 | 2.8 |
| B04 | HiFi 12.8 kb 10x | 1 | 359.7 | 2.8 |
| B05 | ONT 23.8 kb | 1 | 17.8 | 2.8 |
| B06 | AVITI 2x | 32 | 724.6 | 17.9 |
| B07 | AVITI 4x | 32 | 1370.8 | 18.0 |
| B08 | AVITI 7x | 32 | 2337.9 | 18.0 |
| B09 | HG008 1x | 32 | 304.5 | 17.9 |

## Self time by function (% of samples)

Fat LTO inlines most of the pipeline into two frames, so "closure" and `match_rest` stand for
seeding, bucket accumulation, pruning and refinement together; the `-x` stage report is the
tool for splitting those. The other columns are real, separate functions.

| bench | `sketch_slice_into` | index build (`insert_hit` etc.) | `map_reads` closure | `match_rest` | sorting | kernel | allocator |
|---|---:|---:|---:|---:|---:|---:|---:|
| B01 | 20.2 | 8.1 | 20.3 | 21.2 | 4.5 | 9.3 | 2.9 |
| B02 | 22.5 | 9.4 | 21.4 | 12.7 | 5.3 | 10.5 | 3.4 |
| B03 | 16.5 | 7.8 | 21.0 | 19.2 | 5.2 | 9.7 | 4.3 |
| B04 | 12.4 | 2.4 | 28.6 | 26.5 | 6.5 | 4.9 | 4.4 |
| B05 | 29.1 | 12.5 | 11.2 | 5.6 | 4.9 | 14.5 | 4.3 |
| B06 | 0.2 | 1.2 | 25.5 | 28.2 | 19.3 | 1.4 | 5.9 |
| B07 | 0.2 | 0.9 | 26.1 | 28.0 | 19.3 | 1.4 | 5.9 |
| B08 | 0.2 | 0.7 | 26.1 | 28.2 | 19.2 | 1.3 | 6.3 |
| B09 | 0.3 | 1.6 | 27.7 | 25.5 | 18.5 | 1.7 | 6.1 |

## What it shows

- **Long reads, -@1:** sketching is 12-29% (largest on ONT, B05; its share falls with depth
  because mapping work grows faster than sketching). Mapping proper (closure + `match_rest`) is 17-55%, rising
  with depth: B04 spends 55% there. Kernel time is 5-15% (reading the inputs and page faults).
- **Short reads, -@32: sorting is ~19%** in every short-read benchmark, in
  `core::slice::sort::unstable::quicksort` called from bucket merging
  (`Buckets::merge_entries`, `get_sorted_buckets` add another 7%). Long reads spend 4-7% there.
  §11 itemizes only the per-read k-mer sort (~4-5% of mapping, judged not worth pursuing); this
  bucket sort is a different, larger cost on short reads and is not itemized there. It fits
  §11's note that `bucket_merge` and `refine` dominate short reads. Worth a follow-up
  question, not a conclusion: nothing here shows a cheaper sort would preserve output.
- **Short-read sketching shows ~0%**, which is inlining, not absence: it is folded into the
  closure at this thread count. Do not read it as free.
- **Allocator** (`mi_*`, `_mi_page_malloc_zero`) is 3-6%; worth a look on short reads only.

## Limits

Single run per benchmark, one host. perf adds sampling overhead; walls here are not suite
timings and should not be compared with RESULTS.md. Frame pointers and debug info alter code
layout slightly. Self time is cycle-weighted. Symbol names are normalised (hash suffixes
dropped) so one function under several LTO hashes is one row.
