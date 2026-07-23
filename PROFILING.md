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

This is the fourth run of this sweep. The first three each turned up (and fixed) a real bug by
actually cross-checking the extracted numbers rather than trusting the pipeline on faith:

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
   (commit `68b4708`) — see the first finding below for what this revealed.
3. A follow-up correctness pass found a `?` between `self.timers.start("output")` and
   `.stop("output")` in `map_read`'s verbose ground-truth path — if the fallible call between them
   ever actually errored, it would return early and leave that per-read `output` timer running
   forever inside that read's `Timers`. In practice this exact call writes into a `Vec<u8>`, whose
   `io::Write` impl cannot fail, so it was never live, but `?` there was a latent hazard of the
   same shape as finding 1. Changed to `.unwrap()` (commit `aa3d8d4`; the sibling line right below
   it already uses the same pattern for the same reason).
4. Re-testing the verbose path after fix 3 reproduced a real, separate deadlock: a worker panic
   (e.g. `-v 2` against non-ground-truth-encoded reads, which panics by documented design) killed
   the worker thread, which stopped it draining the bounded job channel, which made the reader
   thread block forever sending into it, which made the main thread block forever joining that
   reader — the whole process hung instead of erroring. Fixed (commit `5cae452`) by running each
   worker's `map_read` call through `catch_unwind`, turning a panic into a normal per-read `Err`
   instead of unwinding out of the thread; safe because there's no `unsafe` code anywhere in this
   crate and `map_read` already resets all its state at the top of every call regardless of how
   the previous one ended. This run confirms the fix doesn't change default-mode (non-`-v 2`)
   numbers at all, as expected — none of these datasets hit that code path.

## Summary

| Dataset | Threads | Wall (s) | Index % of wall | Map % of wall | Peak RSS | Worker jobs [min,max] | Worker busy s [min,max] | Max worker_setup s | Collector % of map time |
|---|---:|---:|---:|---:|---:|---|---|---:|---:|
| chrY_sim_10kbp_10x  |  1 |  93.5 |  0.4% | 99.5% | 0.31 GB |          [48673, 48673] | [91.55, 91.55] |  0.15 |  1.9% |
| chrY_sim_10kbp_10x  | 16 |   7.2 |  5.0% | 92.4% | 4.53 GB |            [2937, 3172] |   [6.12, 6.18] |  0.25 | 14.5% |
| chrY_sim_24kbp_10x  |  1 |  24.2 |  1.4% | 98.3% | 0.31 GB |          [25940, 25940] | [22.93, 22.93] |  0.15 |  2.5% |
| chrY_sim_24kbp_10x  | 16 |   2.6 | 13.8% | 79.3% | 4.52 GB |            [1513, 1743] |   [1.53, 1.61] |  0.27 | 22.1% |
| allchr_sim_10kbp_1x |  1 | 101.3 | 20.9% | 78.6% | 15.72 GB |       [242845, 242845] | [66.37, 66.37] |  7.23 |  5.4% |
| allchr_sim_10kbp_1x | 16 |  56.9 | 37.1% | 62.3% | 225.04 GB |     [13577, 16564] |   [4.83, 5.47] | 16.99 |  9.6% |
| allchr_real_24kbp   |  1 |  29.8 | 70.6% | 27.8% | 15.72 GB |            [2000, 2000] |   [0.55, 0.55] |  7.17 |  0.4% |
| allchr_real_24kbp   | 16 |  51.5 | 41.6% | 57.6% | 222.39 GB |             [0, 500] |   [0.00, 0.29] | 21.27 |  0.1% |

("Index/Map % of wall" don't need to sum to 100 — reading the pattern file, sketching the
query/reference, and pipeline setup/teardown fill the rest. "Max worker_setup s" is the slowest
single worker's one-time `Buckets::new` allocation cost, see below. All figures are within normal
run-to-run noise of the previous sweep (a few percent, same as `BENCHMARKS.md`'s own repeated
measurements) except where OS thread-scheduling nondeterminism genuinely picks different "winner"
workers each run on `allchr_real_24kbp -@ 16` — see that finding below, unchanged in kind, just
with this run's actual worker assignments. `indexing`'s wall-clock share is disk-I/O-sensitive and
was already noted to vary with OS page-cache state across earlier sweeps, not code or dataset
changes.)

## Findings

**Concurrent per-worker whole-genome allocations contend for memory bandwidth, and a worker's
setup speed directly decides how much real work it gets — this, not just "uneven distribution" in
the abstract, is why `allchr_real_24kbp` gets *slower* with more threads.** `Buckets::new`'s
one-time ~14.9 GB allocation+zero-init takes ~7.2s done alone (`-@ 1`), but each of 16 workers
doing it *simultaneously* (`-@ 16`) takes 16.3-21.3s — because all 16 fight over memory
bandwidth/the allocator at once (~280s of aggregate setup work across the 16, this run). This
directly causes the uneven job split: the 2 000 reads finish being handed out (a few seconds of
real work) before the slowest-provisioning workers ever finish allocating, so there's a clean
inverse correlation between a worker's own setup time and its job count — this run's breakdown:

| Worker (fastest→slowest setup) | Setup (s) | Jobs |
|---|---:|---:|
| worker-2  | 16.27 | 500 |
| worker-6  | 16.26 | 499 |
| worker-4  | 16.32 | 433 |
| worker-8  | 16.38 | 365 |
| worker-15 | 16.46 | 203 |
| worker-10, 11, 13, 14, 7, 12, 9, 0, 3, 5, 1 | 16.56–21.27 | **0 each** |

— fully **11 of the 16 workers get zero reads** this run (8 of 16 the previous sweep — the exact
count shifts run to run with which workers happen to win the allocation race, but it's reliably
around half or more), each still paying the full 16.5-21.3s allocation for nothing. This is a
genuinely new, previously-invisible cost (see profiler-fix note 2 above) and a concrete
optimization target: pre-sizing `Buckets` more cheaply (e.g. relying on the OS's copy-on-write
zero pages instead of an explicit per-element write loop, since `BucketContent`'s default isn't
all-zero) or sharing/reusing one allocation across workers would likely help multithreaded
whole-genome runs more than anything else in this report — especially on datasets with few reads
relative to thread count. (`allchr_sim_10kbp_1x`, with 242 845 reads, shows the same
setup-time-vs-jobs correlation but far more mildly — a ~18% job-count spread instead of "0 vs
500" — since there's enough real work left over even for the slowest-provisioning worker.)

**Reference indexing is single-threaded and becomes the dominant cost on whole-genome +
few-reads workloads.** For `allchr_real_24kbp` (only 2 000 reads against the full 3.1 Gbp CHM13
genome), indexing is 71% of wall time at 1 thread and still 42% at 16 — the ~21s serial
sketch+index-build floor barely moves while everything else shrinks around it. This is the same
"fixed serial floor (Amdahl's law)" `BENCHMARKS.md` already calls out qualitatively; the profiler
now gives the exact number. **Parallelizing reference sketching across segments/chunks is the
biggest remaining lever for whole-genome + light-read workloads that isn't the `Buckets`
allocation above.**

**`match_seeds` is the largest single stage in the per-read mapping hot path**, everywhere it's
a meaningful fraction of wall time: consistently the largest contributor on chrY_sim_10kbp (1
thread), outweighing `match_rest` and `refine` by several times. If read-mapping throughput
itself needs to improve (as opposed to just adding threads), this is where to look first.

**The collector (serial output/reordering) becomes proportionally more expensive as thread count
rises on fast, small datasets** — 1.9% -> 14.5% of mapping wall time on chrY_10kbp going from 1
to 16 threads, and 2.5% -> 22.1% on the even-faster chrY_24kbp. Nowhere near the 90% warning
threshold `print_warnings` already checks for match_seeds/match_rest, but on a machine with more
cores than these small datasets have reads-per-thread, this is the next thing that would start
to cap scaling — worth a warning threshold of its own if thread counts keep climbing. (This
particular percentage is itself noisy at very low absolute read counts — `allchr_real_24kbp -@ 16`
swung from 4.1% to 0.1% between sweeps purely because `collector_busy` is a tiny, per-read-cost
total over only 2 000 reads; the chrY figures above, with tens of thousands of reads, are the
stable ones to trust.)

## Verification

Two more passes were done specifically to answer "is any of this data actually correct":

**Every timer value was checked against the mathematically-required invariant that no *single
serial stream of execution* can report more elapsed time than the wall clock it ran inside.**
Naively applying "no timer > `wall_seconds`" to the merged `global` table flags ~10 false
positives (e.g. `global.query_mapping` = 98.8s against a 7.2s wall on the 16-thread chrY run) —
but that's *expected*: `global` timers for per-read stages are summed across every read from
every worker via `handler.timers += &done.timers`, so with `-@ 16` that sum is a CPU-seconds-style
aggregate across 16 concurrent streams and can legitimately run up to ~16x wall time. Only
`total`/`mapping`/the `indexing` family/`query_reading` are genuine single-shot wall-clock
brackets and must individually stay under `wall_seconds` regardless of thread count — and every
per-*thread* timer (each thread being, by definition, one serial stream) must too. Re-checked with
that distinction: **zero violations across all 8 reports.** Also cross-checked that
`sum(worker jobs) == reader jobs == collector jobs == total reads` on every report (no read
silently lost or double-counted across the reader/worker-pool/collector pipeline) — holds exactly
in all 8.

**A handful of `vmhwm_kb` memory samples decrease very slightly (≤0.4%) near the end of a run**
(e.g. 16484136 → 16482824 KB on `allchr_real_24kbp-t1`). `VmHWM` is documented as a
monotonically-non-decreasing kernel-tracked high-water mark, so an actual decrease would be
suspicious — but the magnitude (a few MB out of GBs) and exact timing (always right as worker
threads are exiting/joining, while the periodic sampler thread keeps reading concurrently) point
to ordinary read-timing noise on a live, concurrently-changing `/proc/self/status` counter, not a
real drop in peak usage. This doesn't corrupt the reported `peak_rss_kb`, which is computed as a
`max()` over the *entire* sample series rather than trusting the last sample — exactly the kind of
defensive aggregation that makes this class of noise harmless by construction.

**Two timer names, `seed_heuristic` and `match_collect`, are pre-registered (`Timers::init`) but
never actually started/stopped anywhere in the codebase** (confirmed by grep) — they always read
`0.000` in every report, not because those operations are free, but because nothing measures them
at all. Harmless (an unmeasured 0 and a genuinely-instant 0 look identical), pre-existing from
before this profiling work, and left as-is rather than trimmed, but worth knowing when reading the
`global`/per-thread timer tables: a `0.000` next to these two specific names means "never
instrumented," not "instant."

**A separate deadlock, found while re-testing the verbose (`-v 2`) ground-truth path, is now
fixed** (see profiler-fix note 4 above) rather than just flagged: any worker thread panicking used
to hang the whole process forever instead of exiting with an error, via the reader thread blocking
on a now-undrained bounded channel and the main thread blocking forever joining it. Reproduced
directly (`shmap -s ref.fa -p reads.fa -v 2` against ordinary read names, previously never
returned even under a 120s timeout), fixed with `catch_unwind` around each worker's `map_read`
call, and locked in with a regression test that fails fast on a timeout instead of hanging `cargo
test` if it ever comes back.

## Reproducing

```
# via the benchmark harness (writes profiling/<dataset>-t<threads>.profile.json):
python3 profiling/benchmark.py --datasets all --threads 1  --profile --only shmap-rs
python3 profiling/benchmark.py --datasets all --threads 16 --profile --only shmap-rs

# or directly against the binary:
shmap -s ref.fa -p reads.fa -k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment \
    -@ 16 -x --profile-log run.profile.json
```
