# Questions and change requests from Pesho

The running log of what was asked, what was done about it, and what the benchmark said. One
question at a time; one branch and one PR each, so a verdict is attributable to a single change.

Process is in [CONTRIBUTING.md §0](CONTRIBUTING.md). **This file is the index.** Each entry states
the question, the answer in a few lines, and where the evidence lives — the reasoning and the
numbers belong in [RESULTS.md](RESULTS.md) or [PORT_CHANGES.md](PORT_CHANGES.md), not here.

| # | Question | Branch | PR | Status |
|---|---|---|---|---|
| Q1 | Replace `sketch.rs` with an already-optimised library | `q1-sketch-library` | #6 | merged |
| Q2 | Check: does the suite run shmap-rs and map-shmap with Jaccard, Containment, SH? | `q2-three-metrics` | #7 | merged |
| Q3 | Speed and memory vs the C++/paper, verified with C++ source snippets | `q3-optimizations-list` | #8 | merged |
| Q4 | Thread scaling is only ~7x at 64 threads — diagnose and fix | `q4-thread-scaling` | #9 | merged |
| Q5 | Build the NUMA-aware index replication Q4 scoped but didn't ship | `fix-numa-index-replication` | #12 | dropped |
| Q6 | Indexing time vs mapping time, per tool, for third-party comparisons | `q6-index-vs-mapping-time` | #14 | merged |
| Q7 | Optimize the refinement step (it differs for Containment/Jaccard) | `q7-refinement-jaccard-bound` | #16 | merged |
| Q8 | Can SIMD be used in some of the steps of mapping a read? | `q8-simd-mapping-steps` | #17 | merged |
| Q9 | Software prefetching for the pruning lookups | `q9-prefetch-refine` | #18 | merged |
| Q10 | 2-bit-packed sequence encoding for sketching | `q10-2bit-packed-sketching` | #20 | merged |
| Q11 | Visualize the profiling data as charts | `q11-profiling-charts` | #22 | merged |
| Q12 | Map each read at a cheaper threshold first | `q12-adaptive-theta` | #39 | dropped |
| Q13 | Choose the bucket accumulator by occupancy, not by size alone | `q13-dense-occupancy` | — | dropped |
| Q14 | Stop building the per-read seed table nothing reads | `q14-refine-inner-loop` | — | dropped |
| Q15 | Probe the index once per read k-mer, not twice | `q15-single-index-probe` | — | dropped |

Status is one of: **open** (not started) · **in progress** (branch exists) · **in review** (PR open,
awaiting the benchmark) · **merged** · **dropped** (with the reason in its section). An em dash in
the PR column means the work was measured and settled on its branch without one being opened.

**None of the fifteen changed how the mapper computes anything.** Only Q7 touched `src/` at all,
and that was a doc comment citing a section that no longer existed. That is the intended shape:
each was a real hypothesis, probed or built before being accepted or rejected, and a measured
negative is the outcome that keeps it from being retried. The probes are in
[`profiling/`](profiling/) and the verdicts in [RESULTS.md §11](RESULTS.md#11-what-to-try-next).

**Q12–Q15 were dropped for a different reason than Q5**, and the distinction is the useful part.
Q5 net-regressed. These four were *exact and positive* — Q12 measured a full-suite ACCEPT — and
were still not taken, because the merge bar is end-to-end wall time against added complexity, and
a few percent does not clear it. A change can be correct, measured, and not worth its invariants.

---

## Q1 — Replace `sketch.rs` with an already-optimised library

**Asked** 2026-08-02 · `q1-sketch-library` · PR #6 · **merged** 2026-08-02

*„sketch.rs да се замени от библиотека, която вече е оптимизирана."*

**Answer: keep `sketch.rs`, unchanged.** The premise is right — our hash *is* ntHash, verified
window by window against the `nthash` crate at k = 15, 21, 25, 31
([`tests/nthash_equivalence.rs`](tests/nthash_equivalence.rs)). But no library exposes what this
code needs: we canonicalise as `h_fw ^ h_rc` and take the strand from `h_fw > h_rc`, so both hashes
are needed per window; `nthash` keeps them private and yields only `min(h_fw, h_rc)`, and `seq-hash`
implements a different function entirely. Substituting `min` for `xor` changes which k-mers are
sketched — every mapping changes, and `min` clears the threshold about twice as often, silently
doubling sketch density. The crate is also **4.5x slower** (164 against 731 Mbase/s at `-r 0.01`,
k = 25, matched so both select the same k-mers into a pre-reserved buffer).

**Writing SIMD ourselves does not pay either**, and the first answer here was wrong in the direction
that matters. A hash-only probe suggested 1.33x; adding real k-mer emission — verified bit-for-bit
identical to `sketch_slice_into` — measured **0.79–0.87x** at the chunk size that dominates. What
survives is the diagnosis: the loop is **load-bound**, six L1 loads per base, which is why extra
lanes make it slower. See [RESULTS.md §11](RESULTS.md#11-what-to-try-next), where Q10 later
confirmed the same conclusion from the opposite direction.

**Addendum (PR #10) — AVX-512 downclocking.** Checked directly rather than assumed
([`profiling/downclock_probe.rs`](profiling/downclock_probe.rs)). At Q1's own test conditions — one
core busy — scalar and AVX-512 both hold 3900 MHz, so Q1's comparison was not confounded. With all
64 cores busy, scalar sustains 2800 MHz while AVX-512 drops to ~2300–2460 MHz: a **14–18%
package-wide** cut that only appears at production scale. That reinforces the conclusion rather
than reversing it, and is recorded in `benchmarks/results/suite-1.0/x86_64/ARCH.md` as a property of
this host.

**One fact worth keeping.** Stubbing `sketch_slice_into` to a no-op shows sketch compute is 33–50%
of indexing wall — real headroom in principle, just not headroom SIMD can reach on this evidence.

## Q2 — Check: does the suite run both tools with all three metrics?

**Asked** 2026-08-02 · `q2-three-metrics` · PR #7 · **merged** 2026-08-02

*„да пуска shmap-rs и map-shmap с три различни параметъра: Jaccard, Containment, SH"*

**Answer: yes, already satisfied — no change.** Checked rather than assumed: both CLIs accept all
four metrics; all five `[[benchmark]]` blocks in `suite.toml` carry
`metrics = ["Containment", "Jaccard", "bucket_SH"]` and both impls; and the current
`results.tsv` has all six `(impl, metric)` combinations across B01–B05. The C++ binary's `-v 2`
parameter dump prints field names matching the paper's `θ`, `δ`, `ϕ` and Definition 7, confirming it
is the paper's map-shmap under this repo's name for it.

**One piece of dead code found while checking**, flagged rather than fixed: the legacy `Makefile`
`eval_*` pipeline, inherited from the C++ repo, sweeps a `METRIC=fixed_C` that neither binary
accepts, and never touches `Jaccard` at all. Superseded by `run.py`/`suite.toml` for anything this
project gates on.

## Q3 — Speed and memory vs the C++/paper, with verified source snippets

**Asked** 2026-08-02 · `q3-optimizations-list` · PR #8 · **merged** 2026-08-02

*„много подробно обяснение на това какво е сменено … Подредено по значимост."* — a detailed,
significance-ordered account of what changed against the C++ and the paper, with data structures
and reasoning.

**Answer:** [`PORT_CHANGES.md`](PORT_CHANGES.md), which exists because of this question. Every C++
snippet in it is fetched verbatim from upstream at the pinned commit `63f1103` with exact line
numbers, rather than paraphrased from a port-time doc comment — and that fetch corrected the record
twice: `RefSegment::seq`'s comment claims it is "empty if only mapping and no alignment", but
`index.h:104` passes the real sequence unconditionally, so the C++ carries a second full copy of the
genome on every run; and there are two `diff_hist`s, only one of which is on the hot path.

Refined three times by follow-up direction — narrowed to speed and memory specifically, expanded
against an 8-item list of claims to verify, and finally **restructured so a table opens the
document** with one row per optimization and its measured effect. That last rule still applies: a
new optimization adds a row, not just a prose subsection.

## Q4 — Thread scaling caps at ~7x on 64 threads: diagnose, and is rayon the fix?

**Asked** 2026-08-02 · `q4-thread-scaling` · PR #9 · **merged** 2026-08-02

*"scaling with threads doesn't look good. 7x for 64 threads. My colleague dpetrov suggests rayon."*

**Answer: the premise is right, the suggested cause is not.** It is memory-bandwidth contention on
the shared reference index, starting as soon as a second worker joins and accelerating sharply once
`-@` needs a second of this host's four sockets. `rayon` would not fix it — its default pool has no
NUMA awareness, and work-stealing risks making locality worse. Diagnosis in
[RESULTS.md §3](RESULTS.md#3-thread-scaling) and [§11](RESULTS.md#11-what-to-try-next).

Diagnosed in order: the pipeline was ruled out first from counters already in the codebase
(`collector_busy` flat across every thread count, `query_reading` not growing); the real signal is
per-read CPU cost, which rises continuously from `-@2`; `numactl` experiments confirmed it
(`--cpunodebind=0 --membind=0 -@16` matches or beats an unconstrained `-@64`, while `--interleave=all`
does not help, ruling out simple hotspotting); and AVX-512 downclocking was ruled out by `objdump`
finding zero AVX-512 instructions in the shipped binary.

**A correction worth keeping.** The first pass reported efficiency as "~92% flat through 16 threads,
then a cliff" — Pesho asked directly whether that was right, and it was not. That statistic was
`cpu_query_mapping / (threads × wall_mapping)`, which measures whether threads are *busy*, not
whether each read costs what it should; a thread can be 100% busy while doing 28% more work per
read. The corrected metric shows continuous degradation from the second thread on. The cause was
unchanged by the correction — only the shape of the curve.

**Corroborated by hardware, 2026-08-09.** `galaxy` (1 NUMA node) runs the same commit and produces
bit-identical counters, scaling monotonically to 11.7x at `-@64` where a2 peaks at 6.0x on `-@16`
and then declines. The diagnosis no longer rests only on the machine that has the problem. Detail in
[`benchmarks/results/suite-1.0/aarch64/ARCH.md`](benchmarks/results/suite-1.0/aarch64/ARCH.md).

## Q5 — Build the NUMA-aware index replication Q4 scoped but didn't ship

**Asked** 2026-08-03 · `fix-numa-index-replication` · PR #12 · **dropped**

*"Now fix the problem with the poor threading performance"*

**Answer: built five ways, measured, dropped.** Every version of "one copy of the index per NUMA
node" net-regressed against doing nothing at every thread count touching more than one socket —
including the final one that achieves genuinely correct placement by bypassing mimalloc through
`mmap(2)` (B04 `-@64`: 44.5 s replicated against 33.7 s unreplicated). Output stayed byte-identical
throughout; the objection is economic, not correctness. The five attempts and the two mechanisms
that defeated the middle three — `numa_balancing = 1` and mimalloc's arena reuse — are in
[RESULTS.md §11](RESULTS.md#11-what-to-try-next).

**Outcome.** PR #12 closed unmerged; `src/numa.rs`, `src/numa_storage.rs`, `--no-numa` and the
`libc`/`core_affinity` dependencies are all reverted off `main`, so the crate keeps having no
`unsafe`. The record exists so the next person to have this idea starts from "tried, five ways"
rather than from zero. Q4's `numactl` recommendation remains the actionable answer.

## Q6 — Indexing time vs mapping time, per tool

**Asked** 2026-08-03 · `q6-index-vs-mapping-time` · PR #14 · **merged** 2026-08-03

*"run each tool twice — once for measuring the indexing time by mapping only 1 read, and once for
measuring the whole time … subtract."*

**Answer: implemented as the general fallback** for any implementation without a native phase
report. shmap-rs reports the split from its own `-x` instrumentation; `cpp-shmap` now gets it from
Pesho's two-run method inside `run.py`'s `measure()`, so it inherits the same repeat-and-median
treatment as every other reference number. `one_read_fasta()` streams just past the first record
rather than reading a multi-GB reads file, and is unit-tested in CI because it is easy to get subtly
wrong. Verified by hand on B01: index-only 32.65 s, full 107.72 s, derived mapping 75.07 s, with the
index-only PAF carrying exactly one line.

Results in [RESULTS.md §3b](RESULTS.md#3b-index-vs-mapping), which now shows both implementations
rather than the subject alone.

## Q7 — Optimize the refinement step (it differs for Containment/Jaccard)

**Asked** 2026-08-03 · `q7-refinement-jaccard-bound` · PR #16 · **merged**

*"try to optimize the refinement step. it is different for C and J"*

**Answer: no safe change exists — a measured negative, not an unattempted one.** The cost gap is
not that refining is more expensive under Jaccard; per-bucket cost is essentially metric-symmetric
(2.62x time against a 2.70x bucket-count ratio). The whole gap is that Jaccard's pruning lets more
buckets through, and `hseed` is provably the tightest bound obtainable from the counts pruning
tracks. Full mathematical account in [RESULTS.md §11](RESULTS.md#11-what-to-try-next).

Also ruled out: a constant-factor speedup of `best_fixed_length` itself. Its metric dispatch runs
once per outer-loop position, not per k-mer swept, so it is not a meaningful cost either way.

## Q8 — Can SIMD be used in some of the steps of mapping a read?

**Asked** 2026-08-03 · `q8-simd-mapping-steps` · PR #17 · **merged** 2026-08-03

**Answer: no — the dominant per-read costs are the wrong regime for it.** Profiled fresh at the
paper's own parameters rather than reusing k=15 figures: `match_rest`/`refine` is 31.3% of mapping
and `match_seeds` 21.1%, and both are memory-latency-bound and pointer-chasing-heavy. One modest
candidate (the per-read k-mer sort) is identified and not pursued. No `src/` change; detail in
[RESULTS.md §11](RESULTS.md#11-what-to-try-next).

## Q9 — Software prefetching for the pruning lookups

**Asked** 2026-08-03, follow-up to Q8 · `q9-prefetch-refine` · PR #18 · **merged** 2026-08-09

**Answer: probed, measured negative.** Following Q8's finding that the loops are latency-bound, the
textbook remedy was tested at real scale before touching any code
([`profiling/prefetch_probe.rs`](profiling/prefetch_probe.rs)): plain lookahead is 0.64–0.69x and
explicit `_mm_prefetch` at best breaks even. Out-of-order execution already overlaps the independent
loads. The probe answered the question before any real code needed to change. Detail in
[RESULTS.md §11](RESULTS.md#11-what-to-try-next).

## Q10 — 2-bit-packed sequence encoding for sketching

**Asked** 2026-08-09, follow-up to Q9 · `q10-2bit-packed-sketching` · PR #20 · **merged** 2026-08-09

Asked after Q9 whether the run of negative results meant nothing else could be optimized. They did
not — they were four specific hypotheses, not an exhaustive search — and packing was the candidate
worth doing next, being a *different mechanism* from SIMD.

**Answer: probed, measured negative, and the by-product is the more useful result.** Packing loses
even unrolled (1.013 → 1.131 ns/base). A second probe settled why: three independent hash chains buy
only 1.11x, so the loop is not dependency-bound but **load-port-bound at 6 loads/base** — and the
four *LUT* loads are the real cost, so packing was aimed at the wrong half. That independently
confirms Q1's premise from the opposite direction. Detail in
[RESULTS.md §11](RESULTS.md#11-what-to-try-next).

## Q11 — Visualize the profiling data as charts

**Asked** 2026-08-09 · `q11-profiling-charts` · PR #22 · **merged** 2026-08-09

**Answer:** [`benchmarks/scripts/charts.py`](benchmarks/scripts/charts.py) draws `profiles.tsv` as
pie charts — hand-written SVG, so it adds no dependency, diffs in review like the tables do, and
`--check` can detect a stale one byte-for-byte. It reads the *table*, never the raw JSON, so every
wedge traces to a row a reader can look up, and each chart footers the exact row it came from.
Browse them via `chart-index.html` in either architecture's `current/`, linked from the top of
[RESULTS.md](RESULTS.md).

**Outcome.** No `src/` change — this only reads artifacts the benchmark already writes, and the
charts were generated from the existing result set with no re-run, as asked.
[`test_charts.py`](benchmarks/scripts/test_charts.py) pins the aggregation rule, wedge geometry, the
partition guard and end-to-end rendering (30 assertions), wired into CI; `promote.py` carries
`chart-*` so a promoted result set stays viewable.

## Q12 — Map each read at a cheaper threshold first

**Worked** 2026-08-12 · `q12-adaptive-theta` · PR #39 · **dropped**

**The ask was not written down at the time.** Q12–Q15 are a per-read optimization pass that
continues the thread Q10 opened — whether the run of negative results meant nothing else could be
optimized — but no section here can quote what was asked for each, so none of them pretends to.
What each one *did* is recorded below and, except for Q12, in the repository.

**Answer: built, exact, a full-suite ACCEPT — and not merged.** Theta reaches `map_read` in exactly
two places: the seed count `S = (1-(t-d))·m+1` and the bucket sweep's initial threshold. Both get
cheaper as theta rises, and at the paper parameters `S` is 67.5% of a read's k-mer occurrences while
the median HiFi read maps at 0.93 — so nearly every read is charged for a threshold far below the
one it clears. The branch tries one higher rung first and falls back to `-t` only if that finds
nothing.

The early stop is **exact, not an approximation**: every bucket the run at `-t` would consider is
either scored identically at the higher rung, pruned there with a seed-heuristic bound already under
the winning score, or never seeded — and the third case is what `S` is defined to make impossible
for anything scoring near the threshold. Verified over 90 000 reads from four datasets at every
metric against the same binary under `SHMAP_NO_ADAPTIVE_THETA=1`: mapping, scores, mapq, winning
bucket, mapped and mapq-60 counts and B02's ground truth all identical.

**Outcome: ACCEPT, 1.037x end-to-end (1.070x B04, 1.063x B03, 1.003x B05), nothing blocking, C++
agreement unchanged — and PR #39 closed unmerged.** The stages either half can touch move
1.05–1.60x, but they are a minority of wall time everywhere in the suite; against +3.7% the branch
asks the port to carry a second path through `map_read`, a cross-rung reuse contract spanning four
modules, and a tie-escalation rule that exists only to keep output byte-identical.

**Its evidence is on the branch and nowhere else.** Unlike Q13–Q15, whose findings were taken to
`main` as documentation, Q12's 461 lines of RESULTS.md (§5c the ladder, §5d a fitted per-read cost
model) and `profiling/costmodel.py` went with the closed PR. Three things
are therefore only recoverable from `q12-adaptive-theta`, and the branch is kept for that reason:
the cost model itself, which is the tool for judging a perf change on this crate rather than
comparing medians; the finding that **the pruning walk spends 84–92% of its seeds on buckets that
survive it**; and the finding that **the sweep's tie-break is order-dependent**, since adjacent
buckets overlap by design and both can cover a locus with the same intersection. Landing that
material on `main` is separate work and is not done.

## Q13 — Choose the bucket accumulator by occupancy, not by size alone

**Worked** 2026-08-12 · `q13-dense-occupancy` · no PR · **dropped**

**Answer: the premise held, the conclusion did not.** The dense accumulator exists for whole-genome
`k=15`, where a read makes ~4 M contributions against ~242 k buckets and genuinely accumulates. At
the paper's operating point the same reference gives the same slot count from the opposite
direction: a read touches **248 of ~242 000 slots, 0.1% occupancy**, so the array is being used as a
scatter table — and `MAX_DENSE_SLOTS` cannot tell the two cases apart, because they land at the same
size. `plan_dense` was given an occupancy test as well, routing 96.5% of HiFi reads to the sparse
path.

**Outcome: it cancels out.** `match_seeds` 1.143x and `prepare` 1.132x at `-@16` — the
memory-contention half of the rationale confirmed, since it gains more at 16 threads than at 1 —
bought at `bucket_merge` 0.607x, leaving `query_mapping` inside the control cell's noise. At 3.9 MB
the array is L3-resident, so its scatter costs ~40 cycles a miss rather than a DRAM round trip,
which is cheaper per hit than radix-sorting ~3 800 records is per read. Occupancy is the wrong
variable on its own; size against L3 is the right one. Recorded in
[RESULTS.md §11](RESULTS.md#11-what-to-try-next); the branch is kept because the whole loss sits in
a merge tuned for a regime three orders of magnitude larger in entry count.

**A latent panic was found on the way, and is now fixed** — `fix-dense-retire-panic`, PR #43,
merged 2026-08-13. `plan_dense`'s over-size early return replaced `dense` with an empty `Vec` while
`touched` still held ids into the array just dropped, and the retirement loop that would clear them
sat *after* that return. It needed all three of a read abandoned after adding but before extracting,
a following read over `MAX_DENSE_SLOTS`, and a return to the dense path, which is why no run had hit
it; `map_reads` catches per read, so it would have surfaced as one failed read rather than a crash.
Retirement is now a named step at the top of `plan_dense` ([`src/buckets.rs`](src/buckets.rs)), on
the one path every exit passes through, so a future early return cannot reintroduce the hazard by
forgetting to replicate it. `an_abandoned_read_does_not_leak_into_the_next_dense_read` walks the
three steps and fails against the previous ordering with the real message
(`index out of bounds: the len is 0 but the index is 50`). Full suite: **ACCEPT**, 0 failures, all
111 comparable cells identical in `mapped` and `mapq60`.

This is the piece of Q13 that stood alone, as this branch's own commit message said it should. The
occupancy selector it was found under remains unmerged.

## Q14 — Stop building the per-read seed table nothing reads

**Worked** 2026-08-12 · `q14-refine-inner-loop` · no PR · **dropped**

**Answer: real, exact, and too small.** `p_ht` maps every unique read k-mer to a whole `Seed` — a
`Kmer`, three counters and a `PMatches` owning a `Vec`, ~72 bytes — and building it clones each one,
heap allocation included, per read. Its only readers are `lcs` under `bucket_LCS` and the `-v2`
ground-truth analysis, neither of which runs on the three metrics the suite measures. On every
benchmark run this repository has made, it was built per read and never read once. It is now built
only when one of those two will read it; the scoring sweep probes a packed `H2SeedLut` instead.
Measured inside one binary against `SHMAP_FORCE_P_HT=1`, interleaved, median of 4: **`prepare`
1.189x, `query_mapping` 1.028x**, control cell 0.993x. A second change keeps the `f64` divide off
the scoring sweep's common path under plain Containment, where the score is strictly monotone in the
intersection: `refine` 1.024x.

**One hypothesis died here and should not be retried:** shrinking the sweep's k-mer table payload
from ~72 bytes to a packed `u32` measured **nothing** (`refine` 0.997x). At 128 k-mers the fat table
is already near-L1-resident, so size was not what that loop waits on.

**This is also where the measurement method changed.** Comparing two *builds* of this crate
confounds a change with the code layout `lto = "fat"` / `codegen-units = 1` produced: the divide
shortcut reads 1.074–1.152x across builds against 1.024x inside one binary, and
`collect_kmer_info` — which runs before any of it — reproducibly "regressed" 19–23%. A perf claim
here needs one binary and a runtime switch. `SHMAP_FORCE_P_HT` and `SHMAP_NO_SCORE_SHORTCUT` are
that switch and are not scaffolding to delete. That finding is load-bearing for every other
measurement in [RESULTS.md §11](RESULTS.md#11-what-to-try-next).

## Q15 — Probe the index once per read k-mer, not twice

**Worked** 2026-08-12, continuing Q14 · `q15-single-index-probe` · no PR · **dropped**

**Answer: real, exact, and too small — the other half of Q14's ~4%.** `collect_kmer_info` asked the
index how many hits a k-mer has, via `contains_key`, and discarded the entry; `match_seeds` then
re-probed the same shard for the hit itself. Both are cache-missing lookups into the multi-GB
`h2single`/`h2multi` maps. This is the common case, not an edge: of a HiFi read's ~123 unique
sketched k-mers, 96% are unique in the reference, so nearly every one of the ~84 seeds the mapper
consumes per read paid a second lookup for a value the first had already found.
`SketchIndex::lookup` now returns the count and, when it is 1, the hit. Measured inside one binary
against `SHMAP_NO_SEED_HIT_CACHE=1`, median of 4: **`match_seeds` 1.059x, `query_mapping` 1.025x**,
control cell 1.007x, and every stage the change cannot reach flat to within 1%.

**Outcome: with Q14, 1.041x on `query_mapping` — about 1.7% of wall** at `-@1` on a 1x read set, on
one benchmark, metric and thread count. Output byte-identical on 62 148 HiFi reads against the whole
genome for all three metrics in both arms of every switch, with `validate_paf.py` clean. The merge
bar is end to end, so both were recorded rather than merged; of the two, the `p_ht` removal is the
piece that carries its own weight and is independent of the rest.

---

## Template

```
## Q<n> — <short title>

**Asked** <date> · `<branch>` · PR #<n> · **<status>**

**Question.** What Pesho actually asked, in his terms.

**Answer.** What we did or concluded, in a few lines. Link the section of RESULTS.md or
PORT_CHANGES.md that carries the evidence rather than repeating numbers here.

**Outcome.** Benchmark verdict (ACCEPT / REVIEW / BLOCK) and the merge decision.
```

---

# Open with Pesho

Things *we* found that need his input before they can be settled. Not questions from him, so they
do not get a branch until he answers — but they should not be lost either.

## P1 — Which parameter set is authoritative?

The suite runs `-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3` and calls these "the paper parameters". The
draft's own settings section gives `k = 25, r = 0.05, t = 0.5, o = 0.7, d = 0.15` — same `k`, four
different values, and a sampling rate 5x denser.

This is not cosmetic: [RESULTS.md §8](RESULTS.md#8-concordance-with-other-mappers) shows accuracy is
bounded by the sampling rate, so which set is authoritative changes what every table in the file is
a measurement *of*.

## P2 — `lost_on_seeding` / `lost_on_pruning` report nothing

Both are inert in the C++ revision this port was made from: `lost_on_seeding` is a hardcoded `0`,
and `lost_on_pruning` is threaded through `match_rest` as an out-parameter that nothing writes, so
its caller reports a constant 1 per read. shmap-rs ports both as the same inert bumps
(`src/shmap/scoring.rs`, `src/shmap/mod.rs`), which is why the archived profiles carry
`lost_on_pruning` exactly equal to the read count.

The draft's seed-heuristic table reports small non-zero values for "lost on pruning", so those came
from something other than this code path. Which build produced them? Measuring the quantity
properly needs a run with pruning disabled to diff against — see
[RESULTS.md §11](RESULTS.md#11-what-to-try-next).

## P3 — Per-read scaling does not cleanly show the logarithmic term

`fig_time_vs_matches` is instrumented and plotted, but the fits do not settle the claim: against
matches examined, a linear fit beats a logarithmic one on B02 (R² 0.958 vs 0.677) and B05 (0.949 vs
0.763), while B03 goes the other way. That is *not* evidence against `O(R·m·log M)` — the bound's
three per-read factors co-vary, so a single-variable fit misattributes their combined growth.
Isolating the log term needs reads matched on `R` and `m`. Worth agreeing on the right experiment
before the claim appears in print.
