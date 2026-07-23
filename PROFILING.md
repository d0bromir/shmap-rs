# shmap-rs profiling

Generated with the `-x`/`--profile` instrumentation added in `src/profiling.rs`, run via
`profiling/benchmark.py --profile --only shmap-rs` (a copy of the actual benchmark harness used
to reproduce Pesho's Table 1, normally kept at
`~/Pesho/minshmap/realworld/pesho_table1/scripts/benchmark.py` on the 64-core/376 GB benchmark
machine — the same one `BENCHMARKS.md` was measured on). Each of the four Table 1 datasets was
run at `-@ 1` and `-@ 16`; the raw JSON reports are in this directory
(`profiling/<dataset>-t<threads>.profile.json`), alongside the accuracy/timing CSVs
(`table1_t1.csv`, `table1_t16.csv`) the same run produced. Numbers match `BENCHMARKS.md`'s
existing thread-scaling table closely (map time and peak memory both within noise), confirming
`-x` adds no measurable overhead and doesn't perturb the mapping itself.

Only `shmap-rs` was (re-)run here — the other Table 1 mappers (minimap2, winnowmap2, blend,
mapquik, map-shmap, minshmap) have no equivalent instrumentation and their numbers are already
captured in `results/table1_20260718-103540.csv` on the benchmark machine.

This is the third run of this sweep, after two rounds of profiler fixes found by actually
cross-checking the extracted numbers rather than trusting the pipeline on faith:

1. The first run's per-thread "indexer" snapshot cloned the run-wide `total` timer while it was
   still running (it only stops when `Handler` is dropped, after mapping finishes), so a plain
   clone kept advancing with the wall clock until the report was serialized, misreporting
   "indexer" `total` as the *whole program's* wall time. Fixed via `Timer::frozen`/
   `Timers::frozen_snapshot` (commit `6a2c417`). Only affected that one per-thread field, not any
   `global` number.
2. Building `profiling/extract_tables.py` and eyeballing its per-thread breakdown turned up a
   table where every thread's share of wall time summed to well under 100% (`allchr_real_24kbp`
   at 1 thread: 73.8%) — a real ~7.8s gap between the `mapping` phase bracket (8.38s) and every
   named per-read timer on the sole worker summed together (1.43s). Root cause:
   `Buckets::new` allocates one `Vec<BucketContent>` sized from the *whole reference* per worker
   (~14.9 GB for the full CHM13 genome — matching `BENCHMARKS.md`'s independently-observed
   "~14.5 GB per worker thread" memory growth almost exactly), and that multi-second
   allocation+zero-init ran once per worker *before* any per-read timer started, so it was
   completely invisible to the profiler. Fixed by timing it into a new `worker_setup` timer
   (commit `68b4708`) — see the finding below for what this revealed.

## Summary

| Dataset | Threads | Wall (s) | Index % of wall | Map % of wall | Peak RSS | Worker jobs [min,max] | Worker busy s [min,max] | Max worker_setup s | Collector % of map time |
|---|---:|---:|---:|---:|---:|---|---|---:|---:|
| chrY_sim_10kbp_10x  |  1 |  93.5 |  0.4% | 99.6% | 0.31 GB |          [48673, 48673] | [91.64, 91.64] |  0.15 |  1.9% |
| chrY_sim_10kbp_10x  | 16 |   7.2 |  5.0% | 93.2% | 4.53 GB |            [2937, 3121] |   [6.14, 6.21] |  0.28 | 14.7% |
| chrY_sim_24kbp_10x  |  1 |  24.4 |  1.4% | 98.5% | 0.31 GB |          [25940, 25940] | [23.18, 23.18] |  0.16 |  2.4% |
| chrY_sim_24kbp_10x  | 16 |   2.6 | 13.4% | 79.7% | 4.52 GB |            [1505, 1742] |   [1.53, 1.60] |  0.28 | 21.8% |
| allchr_sim_10kbp_1x |  1 | 101.1 | 20.9% | 78.7% | 15.72 GB |       [242845, 242845] | [66.20, 66.20] |  7.24 |  5.4% |
| allchr_sim_10kbp_1x | 16 |  56.1 | 37.7% | 61.7% | 225.06 GB |     [12808, 16494] |   [4.60, 5.34] | 16.41 |  9.3% |
| allchr_real_24kbp   |  1 |  29.6 | 70.3% | 28.0% | 15.72 GB |            [2000, 2000] |   [0.56, 0.56] |  7.18 |  0.4% |
| allchr_real_24kbp   | 16 |  51.3 | 41.7% | 57.4% | 222.65 GB |             [0, 643] |   [0.00, 1.17] | 19.54 |  4.1% |

("Index/Map % of wall" don't need to sum to 100 — reading the pattern file, sketching the
query/reference, and pipeline setup/teardown fill the rest. "Max worker_setup s" is the slowest
single worker's one-time `Buckets::new` allocation cost, see below. `indexing`'s wall-clock share
is disk-I/O-sensitive — this run's `allchr_sim_10kbp_1x` value (20.9%) differs from an earlier
sweep's 33.9% purely because the 3.1 Gbp reference was already OS-page-cached from a prior pass
on this 376 GB machine, not because of any code or dataset change.)

## Findings

**Concurrent per-worker whole-genome allocations contend for memory bandwidth, and this — not
just tiny/uneven read counts — is why `allchr_real_24kbp` gets *slower* with more threads.**
`Buckets::new`'s one-time ~14.9 GB allocation+zero-init takes 7.18s done alone (`-@ 1`), but the
slowest of 16 workers doing it *simultaneously* (`-@ 16`) takes 19.54s — 2.7x longer — because all
16 are fighting over memory bandwidth/the allocator at once. Summed across all 16 workers that's
274s of setup work alone. This is a genuinely new, previously-invisible cost (see the profiler-fix
note above) and a concrete optimization target: pre-sizing `Buckets` more cheaply (e.g. relying on
the OS's copy-on-write zero pages instead of an explicit per-element write loop, since
`BucketContent`'s default isn't all-zero) or sharing/reusing one allocation across workers would
likely help multithreaded whole-genome runs more than anything else in this report.

**Reference indexing is single-threaded and becomes the dominant cost on whole-genome +
few-reads workloads.** For `allchr_real_24kbp` (only 2 000 reads against the full 3.1 Gbp CHM13
genome), indexing is 70% of wall time at 1 thread and still 42% at 16 — the ~21s serial
sketch+index-build floor barely moves while everything else shrinks around it. This is the same
"fixed serial floor (Amdahl's law)" `BENCHMARKS.md` already calls out qualitatively; the profiler
now gives the exact number. **Parallelizing reference sketching across segments/chunks is the
biggest remaining lever for whole-genome + light-read workloads that isn't the `Buckets`
allocation above.**

**`match_seeds` is the largest single stage in the per-read mapping hot path**, everywhere it's
a meaningful fraction of wall time: 61.8s of 93.5s total (chrY_sim_10kbp, 1 thread), consistently
outweighing `match_rest` and `refine` by several times. If read-mapping throughput itself needs to
improve (as opposed to just adding threads), this is where to look first.

**The collector (serial output/reordering) becomes proportionally more expensive as thread count
rises on fast, small datasets** — 1.9% -> 14.7% of mapping wall time on chrY_10kbp going from 1
to 16 threads, and 2.4% -> 21.8% on the even-faster chrY_24kbp. Nowhere near the 90% warning
threshold `print_warnings` already checks for match_seeds/match_rest, but on a machine with more
cores than these small datasets have reads-per-thread, this is the next thing that would start
to cap scaling — worth a warning threshold of its own if thread counts keep climbing.

**At high thread-to-read-count ratios, per-worker job distribution can be very uneven.**
`allchr_real_24kbp` at 16 threads split its 2 000 reads as unevenly as 0 to 643 jobs per worker
(one worker got no reads at all), compounding the allocation-contention effect above as the reason
`-@ 16` was *slower* than `-@ 1` on this dataset (51.3s vs 29.6s wall) — consistent with
`BENCHMARKS.md`'s existing note that `allchr real 24kbp` "actually gets slower with more threads"
on datasets too small to amortize thread/channel overhead.

**Reference indexing is single-threaded and becomes the dominant cost on whole-genome +
few-reads workloads.** For `allchr_real_24kbp` (only 2 000 reads against the full 3.1 Gbp CHM13
genome), indexing is 71% of wall time at 1 thread and still 42% at 16 — the ~21s serial
sketch+index-build floor (`indexing` timer: 21.15s at 1 thread, 21.29s at 16) barely moves while
everything else shrinks around it. This is the same "fixed serial floor (Amdahl's law)"
`BENCHMARKS.md` already calls out qualitatively; the profiler now gives the exact number.
**Parallelizing reference sketching across segments/chunks is the single biggest remaining lever
for whole-genome + light-read workloads.**

**`match_seeds` is the largest single stage in the per-read mapping hot path**, everywhere it's
a meaningful fraction of wall time: 61.8s of 93.3s total (chrY_sim_10kbp, 1 thread) and 66.8s
(summed across 16 workers) of the 16-thread run. It consistently outweighs `match_rest` and
`refine` by several times. If read-mapping throughput itself needs to improve (as opposed to
just adding threads), this is where to look first.

**The collector (serial output/reordering) becomes proportionally more expensive as thread count
rises on fast, small datasets** — 1.9% -> 14.1% of mapping wall time on chrY_10kbp going from 1
to 16 threads, and 2.4% -> 21.0% on the even-faster chrY_24kbp. Nowhere near the 90% warning
threshold `print_warnings` already checks for match_seeds/match_rest, but on a machine with more
cores than these small datasets have reads-per-thread, this is the next thing that would start
to cap scaling — worth a warning threshold of its own if thread counts keep climbing.

**At high thread-to-read-count ratios, per-worker job distribution can be very uneven.**
`allchr_real_24kbp` at 16 threads split its 2 000 reads as unevenly as 0 to 509 jobs per worker
(one worker got no reads at all), and this exact run is also the one place in the whole sweep
where `-@ 16` was *slower* than `-@ 1` (50.3s vs 29.8s wall) — consistent with `BENCHMARKS.md`'s
existing note that `allchr real 24kbp` "actually gets slower with more threads" on datasets too
small to amortize thread/channel overhead. The profiler now shows why directly: with indexing
unchanged (~21s either way) and the mapping workload itself tiny and unevenly distributed, the
added synchronization cost has nothing to pay for itself with.

## Reproducing

```
# via the benchmark harness (writes profiling/<dataset>-t<threads>.profile.json):
python3 profiling/benchmark.py --datasets all --threads 1  --profile --only shmap-rs
python3 profiling/benchmark.py --datasets all --threads 16 --profile --only shmap-rs

# or directly against the binary:
shmap -s ref.fa -p reads.fa -k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment \
    -@ 16 -x --profile-log run.profile.json
```
