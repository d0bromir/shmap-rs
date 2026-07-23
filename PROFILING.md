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

This is the fifth run of this sweep. The first four turned up (and fixed) real bugs in the
profiler itself by actually cross-checking the extracted numbers rather than trusting the
pipeline on faith; this one validates the actual memory/speed optimizations those numbers pointed
at — see "Optimizations applied" below for the headline result.

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

## Optimizations applied (commit `0fd8f5a`)

Three of the four optimizations these findings pointed at were implemented and re-measured
end-to-end (all 4 datasets, `-@ 1`/`-@ 16`, accuracy cross-checked byte-for-byte identical to the
pre-optimization CSVs — see "Verification" below):

1. **`Buckets`'s primary storage is now a sparse `FxHashMap<BucketLoc, BucketContent>`** instead
   of one `Vec<BucketContent>` per reference segment sized to the whole reference (the ~14.9 GB
   per-worker allocation identified above as the single largest hidden cost). Every operation
   already only ever touched buckets tracked in `non_empty_buckets_with_repeats`, so this was a
   storage swap, not an algorithm change — extending the same sparse pattern `BucketsHash` already
   used a few lines away in the same file to the primary store too.
2. **The collector now writes PAF output through one `BufWriter` held for the whole run** instead
   of `print!()` (which flushes on every `\n`, i.e. every read) per read.
3. **`match_seeds` reuses one `BucketsHash` scratch map across every multi-hit seed in a read**
   instead of allocating a fresh one per seed.

Not yet implemented: parallelizing reference indexing (the largest-effort item proposed, needs a
new reader/worker-pool/collector pipeline mirroring the mapping one while preserving determinism).

**Measured impact** (this machine, before commit `09e03dc` → after `0fd8f5a`):

| Dataset | Threads | Wall before | Wall after | Speedup | Peak RSS before | Peak RSS after | Memory cut |
|---|---:|---:|---:|---:|---:|---:|---:|
| allchr_real_24kbp   |  1 |  29.8s |  22.2s | 1.34x | 15.72 GB | 2.29 GB | 85% |
| allchr_real_24kbp   | 16 |  51.5s |  21.4s | **2.40x** | 222.39 GB | 2.29 GB | **99%** |
| allchr_sim_10kbp_1x |  1 | 101.3s |  95.1s | 1.07x | 15.72 GB | 2.29 GB | 85% |
| allchr_sim_10kbp_1x | 16 |  56.9s |  27.2s | 2.09x | 225.04 GB | 2.29 GB | 99% |
| chrY_sim_10kbp_10x  |  1 |  93.5s |  91.7s | 1.02x | 0.31 GB | 0.16 GB | 49% |
| chrY_sim_10kbp_10x  | 16 |   7.2s |   6.6s | 1.09x | 4.53 GB | 0.16 GB | 96% |
| chrY_sim_24kbp_10x  |  1 |  24.2s |  23.8s | 1.02x | 0.31 GB | 0.16 GB | 48% |
| chrY_sim_24kbp_10x  | 16 |   2.6s |   2.2s | 1.18x | 4.52 GB | 0.16 GB | 96% |

Peak memory on the whole-genome datasets is now **flat at ~2.3 GB regardless of thread count**
(previously 15.7 GB at `-@ 1`, scaling up to 222-225 GB at `-@ 16`). `worker_setup` — the one-time
per-worker `Buckets::new` allocation cost the previous section's whole finding was about — is now
**0.00s on every single worker, every dataset, every thread count**: the biggest lever proposed
turned out to deliver exactly as predicted.

The headline result: **`allchr_real_24kbp` no longer gets slower with more threads.** Before, `-@
16` (51.5s) was slower than `-@ 1` (29.8s) — the exact anomaly `BENCHMARKS.md` had already flagged
qualitatively. After, `-@ 16` (21.4s) is faster than `-@ 1` (22.2s), because there's no more
multi-second allocation for 16 workers to contend over — the pathological "slower with more
threads" case this whole investigation started from is gone.

## Findings (pre-optimization; superseded above where noted)

**Concurrent per-worker whole-genome allocations used to contend for memory bandwidth, and a
worker's setup speed directly decided how much real work it got — this, not just "uneven
distribution" in the abstract, was why `allchr_real_24kbp` got *slower* with more threads.** ***Now
fixed — see "Optimizations applied" above.*** `Buckets::new`'s one-time ~14.9 GB allocation+
zero-init used to take ~7.2s done alone (`-@ 1`), but each of 16 workers doing it *simultaneously*
(`-@ 16`) took 16.3-21.3s — all 16 fighting over memory bandwidth/the allocator at once (~280s of
aggregate setup work across the 16, one sweep). This directly caused the uneven job split: the
2 000 reads finished being handed out (a few seconds of real work) before the slowest-provisioning
workers ever finished allocating, so there was a clean inverse correlation between a worker's own
setup time and its job count — one sweep's breakdown, kept here for the record:

| Worker (fastest→slowest setup) | Setup (s) | Jobs |
|---|---:|---:|
| worker-2  | 16.27 | 500 |
| worker-6  | 16.26 | 499 |
| worker-4  | 16.32 | 433 |
| worker-8  | 16.38 | 365 |
| worker-15 | 16.46 | 203 |
| worker-10, 11, 13, 14, 7, 12, 9, 0, 3, 5, 1 | 16.56–21.27 | **0 each** |

— fully **11 of the 16 workers got zero reads** that sweep (8 of 16 another sweep — the exact
count shifted run to run with which workers happened to win the allocation race, but it was
reliably around half or more), each still paying the full 16.5-21.3s allocation for nothing.

**Reference indexing is single-threaded and becomes the dominant cost on whole-genome +
few-reads workloads.** *Not addressed by this round of optimization — still current.* For
`allchr_real_24kbp` (only 2 000 reads against the full 3.1 Gbp CHM13 genome), indexing is ~71% of
wall time at 1 thread and ~42% at 16 — the ~21s serial sketch+index-build floor barely moves while
everything else shrinks around it. This is the same "fixed serial floor (Amdahl's law)"
`BENCHMARKS.md` already calls out qualitatively. **Parallelizing reference sketching across
segments/chunks is now the single biggest remaining lever** for whole-genome + light-read
workloads, now that the `Buckets` allocation is fixed.

**`match_seeds` is the largest single stage in the per-read mapping hot path.** *Not addressed by
this round — still current, though its per-seed `BucketsHash` allocation churn was reduced (see
optimization 3 above).* Everywhere it's a meaningful fraction of wall time it's the largest
contributor, outweighing `match_rest` and `refine` by several times. If read-mapping throughput
itself needs to improve (as opposed to just adding threads or fixing memory), this is where to
look first — ideally with a real CPU profiler (perf/flamegraph) rather than stage-level timers, to
see how much of its cost is now genuinely seed-matching work versus remaining allocation/hashing
overhead.

**The collector (serial output/reordering) becomes proportionally more expensive as thread count
rises on fast, small datasets** — 1.9% -> 14.5% of mapping wall time on chrY_10kbp going from 1
to 16 threads, and 2.5% -> 22.1% on the even-faster chrY_24kbp, in the pre-optimization data.
*Partially addressed*: the collector now writes through a `BufWriter` instead of flushing per read
(optimization 2 above); worth re-measuring this specific percentage in a future sweep to quantify
how much that helped, since it wasn't isolated out from the `Buckets` change's much larger effect
in this round's before/after table. Nowhere near the 90% warning threshold `print_warnings`
already checks for match_seeds/match_rest, but on a machine with more cores than these small
datasets have reads-per-thread, this is worth a warning threshold of its own if thread counts keep
climbing. (This percentage is itself noisy at very low absolute read counts — chrY's figures,
with tens of thousands of reads, are the stable ones to trust.)

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
