# Flame graphs, 2.0.0, galaxy (aarch64)

Same run as the [a2 (x86_64) set](../flamegraphs-2.0.0-x86_64/README.md), same commit, script and
parameters; read that README for method and limits. **A diagnostic, not a measurement.**

| | |
|---|---|
| commit | `19e55af711fa599e79fb07d98b099be8a1b8a030` (tag `2.0.0`) |
| host | `galaxy`, 128-core aarch64, `perf` 6.17.13, shared (load 2-4: GATK + ollama), bench lock held |
| sampling | `perf record -F 99 --call-graph fp`, user + kernel, `kernel.perf_event_paranoid=1` |
| threads | `-@1` for B01-B05; `-@32` for B06-B09 |
| files | `B0N.svg`, `B0N.top.txt`, `B0N.time`, `top-frames.md`, `summary.tsv`, `RUN.txt`; `perf.data` and stderr not kept |

## Wall time under perf

| bench | wall s | peak RSS GB | | bench | wall s | peak RSS GB |
|---|---:|---:|---|---|---:|---:|
| B01 | 41.8 | 2.8 | | B06 | 554.3 | 18.0 |
| B02 | 34.0 | 2.6 | | B07 | 1102.9 | 18.0 |
| B03 | 46.7 | 2.6 | | B08 | 1931.4 | 18.0 |
| B04 | 412.7 | 2.8 | | B09 | 281.3 | 17.9 |
| B05 | 22.0 | 2.5 | | | | |

## Self time by function (% of samples)

| bench | `sketch_slice_into` | index build + hashing | `map_reads` closure | `match_rest` | sorting | kernel | allocator |
|---|---:|---:|---:|---:|---:|---:|---:|
| B01 | 23.4 | 11.0 | 21.3 | 20.9 | 4.5 | 4.8 | 3.3 |
| B02 | 27.2 | 12.7 | 23.4 | 13.3 | 4.3 | 4.8 | 3.0 |
| B03 | 20.9 | 11.0 | 23.3 | 19.1 | 4.9 | 4.5 | 3.7 |
| B04 | 14.2 | 6.1 | 29.2 | 24.2 | 6.0 | 4.8 | 4.5 |
| B05 | 33.1 | 15.6 | 13.6 | 5.8 | 3.3 | 11.1 | 3.0 |
| B06 | 0.2 | 3.8 | 27.5 | 27.2 | 23.9 | 1.1 | 5.2 |
| B07 | 0.2 | 3.5 | 27.7 | 27.2 | 24.2 | 0.7 | 5.3 |
| B08 | 0.2 | 3.4 | 27.6 | 27.2 | 24.3 | 0.7 | 5.4 |
| B09 | 0.3 | 4.5 | 29.3 | 23.6 | 23.9 | 0.8 | 5.5 |

Columns match a2 except "index build + hashing", which here sums every `index::`, `hashbrown`
and `core::hash` frame, so it is larger than a2's "index build" column; the remaining columns
use a2's definitions. "Sorting" is every `core::slice::sort` frame.

## Versus a2

- **Same shape.** Mapping closure + `match_rest` is ~50-55% on short reads and 17-55% on long
  reads; sketching is 14-33% on long reads and largest on ONT (B05).
- **Short-read sorting is ~24% here vs ~19% on a2**; same functions (bucket-merge sorts and
  `Buckets::merge_entries`/`get_sorted_buckets`, another ~7%). One run each, so the gap is not
  explained. aarch64 symbols keep generic arguments, so the cost is attributable:
  `(i32,i32,i32)` sort keyed in `best_containment_window_via_anchors` ~8.5%, `u64` ~6%, `u32` ~4%.
- **Kernel time is lower** on long reads (~5% vs 9-10% on B01-B03); cause not investigated.
- Walls are not comparable with a2: different CPU, and galaxy was shared. Short reads ran
  *faster* here (B06 554 s vs 725 s) at `-@32` on 128 cores.
