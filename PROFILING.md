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

This is the second (corrected) run of this sweep: the first turned up a bug in the profiler
itself — the per-thread "indexer" snapshot cloned the run-wide `total` timer while it was still
running (it only stops when `Handler` is dropped, after mapping finishes), so a plain clone kept
advancing with the wall clock until the report was finally serialized, misreporting "indexer"
`total` as the *whole program's* wall time instead of how long had elapsed when indexing
actually finished. Fixed in `src/utils.rs`/`src/index.rs` (`Timer::frozen`/`Timers::frozen_snapshot`,
commit `6a2c417`) and re-measured below; it only affected that one per-thread field, not any of
the `global` numbers already summarized here.

## Summary

| Dataset | Threads | Wall (s) | Index % of wall | Map % of wall | Peak RSS | Worker jobs [min,max] | Worker busy s [min,max] | Collector % of map time |
|---|---:|---:|---:|---:|---:|---|---|---:|
| chrY_sim_10kbp_10x  |  1 |  93.3 |  0.4% | 99.6% | 0.31 GB |          [48673, 48673] | [91.42, 91.42] |  1.9% |
| chrY_sim_10kbp_10x  | 16 |   7.2 |  5.0% | 92.9% | 4.53 GB |            [2968, 3176] |   [6.16, 6.20] | 14.1% |
| chrY_sim_24kbp_10x  |  1 |  24.2 |  1.4% | 98.4% | 0.31 GB |          [25940, 25940] | [22.94, 22.94] |  2.4% |
| chrY_sim_24kbp_10x  | 16 |   2.4 | 14.6% | 84.5% | 4.52 GB |            [1452, 1747] |   [1.52, 1.58] | 21.0% |
| allchr_sim_10kbp_1x |  1 | 100.7 | 20.8% | 78.8% | 15.72 GB |       [242845, 242845] | [66.04, 66.04] |  5.5% |
| allchr_sim_10kbp_1x | 16 |  56.9 | 37.3% | 62.1% | 225.05 GB |     [13596, 16895] |   [4.81, 5.52] |  9.4% |
| allchr_real_24kbp   |  1 |  29.8 | 70.9% | 28.1% | 15.72 GB |            [2000, 2000] |   [0.54, 0.54] |  0.4% |
| allchr_real_24kbp   | 16 |  50.3 | 42.4% | 56.9% | 221.42 GB |             [0, 509] |   [0.00, 0.29] |  0.2% |

("Index/Map % of wall" don't need to sum to 100 — reading the pattern file, sketching the
query/reference, and pipeline setup/teardown fill the rest. `allchr_sim_10kbp_1x`'s 1-thread
`indexing` share dropped noticeably from the first sweep to this one — this machine has 376 GB
RAM and the 3.1 Gbp reference had already been read once earlier in the session, so the second
pass largely hit the OS page cache instead of disk; `indexing` itself is disk-I/O-sensitive and
its wall-clock share will vary with cache state, not just dataset/thread count.)

## Findings

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
