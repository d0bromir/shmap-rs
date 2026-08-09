# Questions and change requests from Pesho

The running log of what was asked, what was done about it, and what the benchmark said. One
question at a time; one branch and one PR each, so a verdict is attributable to a single change.

Process is in [CONTRIBUTING.md §0](CONTRIBUTING.md). Keep entries short — the reasoning belongs in
[RESULTS.md](RESULTS.md) or the commit message, and this file is the index.

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
| Q9 | Software prefetching for the pruning lookups | `q9-prefetch-refine` | #18 | in review |
| Q10 | 2-bit-packed sequence encoding for sketching | `q10-2bit-packed-sketching` | #20 | in review |

Status is one of: **open** (not started) · **in progress** (branch exists) · **in review** (PR open,
awaiting the benchmark) · **merged** · **dropped** (with the reason in its section).

---

## Q1 — Replace `sketch.rs` with an already-optimised library

**Asked** 2026-08-02 · **Branch** `q1-sketch-library` (merged) · **PR** #6 · **Status** merged 2026-08-02

**Question.** *„sketch.rs да се замени от библиотека, която вече е оптимизирана."* — replace our
sketching code with an existing, already-optimised library instead of maintaining our own.

**Answer.** Keep `sketch.rs`, unchanged. The library exists and the hash is genuinely standard, but
swapping it in would change every mapping *and* run 4.5x slower. Writing SIMD ourselves does not pay
either — the first pass looked like a 1.33x win, but that measured hashing only; a second pass added
real k-mer emission, verified correctness bit-for-bit against the real code, and found it 0.79–0.96x
— a wash to a regression, not a win.

Three findings on the library question, each pinned by a test in
[`tests/nthash_equivalence.rs`](tests/nthash_equivalence.rs):

1. **Our hash *is* ntHash.** The four base constants and the rolling scheme are identical to the
   `nthash` crate's, verified window by window against `ntf64`/`ntr64` at k = 15, 21, 25, 31. So
   the premise of the question is right: this is a standard function, not a bespoke one.

2. **No library exposes what we need.** We canonicalise as `h_fw ^ h_rc` and take the strand from
   `h_fw > h_rc`, so both hashes are needed per window. `nthash` keeps them private and yields only
   `min(h_fw, h_rc)`; `seq-hash` (the SIMD one) is a *different* function — 32-bit hashes with a
   rotate-by-7 scheme, not 64-bit rotate-by-1. Substituting `min` for `xor` changes which k-mers are
   sketched, so every mapping changes and the merge gate blocks it. `min` also clears a threshold
   about twice as often as `xor`, so a naive swap silently doubles sketch density too.

3. **The crate is 4.5x slower.** At `-r 0.01`, k = 25, on 20 Mbase, matched so both sides select the
   same number of k-mers and push them into a pre-reserved buffer:

   | | throughput |
   |---|---:|
   | `sketch.rs` | **731 Mbase/s** |
   | `nthash` crate | 164 Mbase/s |

   The gap is the optimisation already in `sketch.rs`: the fixed rotates are pre-baked into the
   lookup tables and the hot loop walks incoming and outgoing bases as zipped slice iterators, so
   there are no bounds checks and no sign extensions per base. The crate's iterator does the rotates
   per step. Getting the comparison fair mattered more than running it — at `h_frac = 1.0` against a
   checksum fold, the crate looks 2x *faster*, because that measures our `Vec` growth rather than
   anyone's hashing.

### What the literature says

Surveyed after the crate comparison, to check the answer is not just "no crate" but "no known
method":

- **ntHash** ([Mohamadi et al. 2016](https://doi.org/10.1093/bioinformatics/btw397)) — the
  algorithm we implement. Reference C++ at [bcgsc/ntHash](https://github.com/bcgsc/ntHash).
- **SimdMinimizers** ([Groot Koerkamp & Marchet
  2025](https://www.biorxiv.org/content/10.1101/2025.01.27.634998v2.full)) — the state of the art
  for vectorised ntHash, and the most useful source here. Their own write-up records that the
  AVX2 SIMD version *lost* to scalar because reading the sequence characters and looking up their
  hashes needs too many instructions, and names the missing AVX-512 lookup instruction as what
  would fix it. Their fastest form is parallel *scalar*, ~3 cycles/k-mer.
- **sourmash / branchwater** ([Irber et al.
  2022](https://www.biorxiv.org/content/10.1101/2022.01.11.475838.full.pdf)) — the reference
  FracMinHash implementation, but it hashes with MurmurHash3 and is not rolling: a different
  function *and* a slower one.

### Then we tried SIMD ourselves — twice, because the first attempt was measuring the wrong thing

The host has AVX-512, so the instruction SimdMinimizers lacked (`vpermq`, a table lookup in a
register) is available. First pass, hashing only — no k-mer records produced, just a checksum and a
count of windows clearing the threshold —
[`profiling/sketch_simd_probe.rs`](profiling/sketch_simd_probe.rs) and
[`profiling/sketch_lanes_probe.rs`](profiling/sketch_lanes_probe.rs), k = 25, 50 Mbase:

| variant | throughput (hashing only) |
|---|---:|
| scalar, as shipped | 727 Mbase/s |
| multi-lane scalar (L = 2…16) | 382–645 Mbase/s — **all slower** |
| AVX-512, 8 lanes, gathered | 427 Mbase/s |
| AVX-512, 8 lanes, transposed input | **3011** hashing only; 587 with the transpose; 441 with the ASCII→code pass too |
| AVX-512, 8 lanes, eight scalar loads, no prep | **969 Mbase/s** |

**The loop is not latency-bound, it is load-bound.** Six L1 loads per base — two sequence bytes and
four table entries. That is why adding independent lanes makes it *slower*: more lanes means
proportionally more loads, and there was no stalled dependency chain to fill. That finding still
holds — it is the *speedup* estimate built on top of it that did not.

**The 969 Mbase/s / 1.33x figure was wrong, and it was wrong in the direction that matters.**
"Hashing only" does not produce a `Kmer` — no position, no strand, nothing pushed anywhere — and
that is exactly the cost a real sketcher cannot skip. So a second pass added it:
[`tests/avx_emit_probe.rs`](tests/avx_emit_probe.rs) extracts each lane's `{position, hash,
strand}` on the ~1-in-13 step where any of the 8 lanes clears the `-r 0.01` threshold, pushes a real
`Kmer` into that lane's own buffer, and concatenates the eight buffers at the end. **Its output is
verified bit-for-bit identical to the real `FracMinHash::sketch_slice_into`** — same positions, same
hashes, same strand bits, on chunk sizes matching what `chunk_windows` actually hands a worker
(2²¹ windows, the `-@64` floor; ~97 Mbase, the `-@8` size):

| chunk size | speedup, 8 repeats |
|---|---|
| ~2.1 M windows (`-@64` floor) | 0.84–1.08x — a wash, noisy at this size |
| ~97 Mbase (`-@8` chunk) | **0.79–0.87x — consistently slower, never a win** |

**The correctness-verified AVX-512 sketcher does not beat the scalar code it would replace, and
loses outright at the chunk size that matters most.** `chunk_windows` (`src/index.rs`) only returns
its `MIN_CHUNK` floor (2²¹ windows) at very high thread counts on this reference — most of the
thread-count range indexes in chunks closer to the 97 Mbase size, where AVX-512 is reliably 13–21%
slower. The extraction that a hash-only measurement has no reason to include — three
`_mm512_storeu_si512` stores and a per-lane loop, paid on every step that selects at least one of 8
lanes (~7.7% of steps at `-r 0.01`,
by 1 − 0.99⁸) — costs more than the 4.1x-faster vector core saves.

**A real thing was built and measured, and the honest answer reverses the earlier estimate: SIMD
does not help `sketch.rs`, full stop.** Not "1.33x, not worth the complexity" — 0.8x, a regression,
even setting complexity aside. `profiling/sketch_simd_probe.rs` and
`profiling/sketch_lanes_probe.rs` are kept because the load-bound finding is still correct and
useful context; their throughput numbers are hash-only and are superseded by
`tests/avx_emit_probe.rs` for anything about real sketching speed.

**The earlier "reader emits lane-major codes" follow-up is retracted.** It was reasoning from the
hash-only number and assumed the transpose was the only cost standing between the code and the 4x
vector core. It was not — the winning hash-only variant already needed no reader change (eight
ordinary scalar loads against the existing ASCII buffer), and even that loses once it does real
work. There is no cheap fix on the table; extracting a scattered, low-probability selection from a
SIMD register is inherently what it costs.

**One fact from this investigation is worth keeping for later, though it points nowhere yet.**
Stubbing `sketch_slice_into` to a no-op (env-gated, never committed) and measuring real `-x`
profiling on the actual reference (`REF-HS1`, `-@1/8/64`) shows sketch compute is 33–50% of
indexing wall — not the small residue "indexing bottoms out near 3s... further gains need reading
less" in §5 of RESULTS.md would suggest to a casual reading. That is real headroom in principle. It
is just not headroom SIMD can reach, on this evidence.

**Outcome.** No change to `sketch.rs`. The PR adds the test that pins the library finding and the
two probes that pin the SIMD one, so both stay checkable and nobody re-derives them.

### Addendum, 2026-08-02 (PR #10) — checked whether Cascade Lake downclocking confounds the SIMD numbers

Raised mid-Q4 (this host's CPUs, Xeon Gold 5218, are Cascade Lake — well documented to throttle
under sustained multi-core AVX-512). Worth checking directly rather than assuming it either
invalidates or doesn't affect the numbers above, since `turbostat` itself needs root/MSR access
this account doesn't have — `/proc/cpuinfo`'s per-core MHz field doesn't, so
[`profiling/downclock_probe.rs`](profiling/downclock_probe.rs) uses that instead: one thread per
core running a real (non-optimizable-away) loop for a few seconds, `/proc/cpuinfo` sampled every
200ms.

**Q1's single-threaded comparison was not itself confounded.** With only 1 core busy and the
other 63 idle — Q1's actual test conditions — both scalar and AVX-512 hold the full single-core
turbo ceiling, 3900 MHz, identically, reproduced twice. No frequency penalty at the scale Q1
actually measured at.

**But there is a real, separate, larger penalty — and it only shows up at production scale.**
With all 64 cores busy simultaneously, scalar sustains a flat 2800 MHz; the same 64 cores running
AVX-512 drop to ~2300–2460 MHz — once landing exactly on the 2300 MHz base clock, zero turbo at
all. A 14–18% package-wide frequency cut, reproduced twice. This is exactly the condition that
would occur if AVX-512 sketching had been adopted and every worker thread ran it concurrently at
high `-@` — a cost no single-threaded microbenchmark, including Q1's, could ever see.

**This reinforces the original conclusion; it doesn't reverse it.** The correctness-verified
AVX-512 sketcher already lost to scalar (0.79–0.87x) *before* accounting for this. Deployed at
`-@64` in production, it would additionally have cost the ~14–18% package-wide clock penalty just
measured — on top of an already-losing comparison, and potentially dragging down every *other*
concurrently-running worker thread's non-AVX-512 work too, not only the sketching itself. Recorded
as project memory (`no-avx512-cascade-lake.md`) so any future SIMD proposal on this hardware is
judged against the multi-core number, not just an isolated single-core comparison.

---

## Q2 — Check: does the suite already run shmap-rs and map-shmap with three metrics?

**Asked** 2026-08-02 · **Branch** `q2-three-metrics` (merged) · **PR** #7 · **Status** merged 2026-08-02

**Question.** *„да пуска shmap-rs и map-shmap с три различни параметъра: Jaccard, Containment, SH"*
— check whether this is already satisfied: does the project run both shmap-rs and map-shmap (the
paper's tool, C++, this repo's `cpp-shmap`) under all three metrics — Jaccard, Containment, and
`bucket_SH` ("SH")?

**Answer. Yes, already satisfied — no code change.** The current benchmark suite has run both
implementations under all three metrics for every result set it has ever produced.

**Evidence, checked rather than assumed:**

1. **Both CLIs already accept all three.** shmap-rs's `-m` is a `clap::ValueEnum`
   (`src/types.rs`) with exactly `Containment | Jaccard | bucket_SH | bucket_LCS`. The C++
   binary's own `--help` prints `-m metric   Optimization metric: bucket_SH, bucket_LCS,
   Containment, Jaccard` — the same four, and its `-v 2` parameter dump prints field names
   (`tThres`, `min_diff`, `max_overlap`, `metric`) matching the paper's `θ`, `δ`, `ϕ` and its
   Definition 7 exactly, confirming this binary *is* the paper's map-shmap under this repo's name
   for it (`cpp-shmap`), not a different tool.

2. **`suite.toml` already configures every benchmark this way.** All five `[[benchmark]]` blocks
   (B01–B05) carry `metrics = ["Containment", "Jaccard", "bucket_SH"]` and
   `impls = ["shmap-rs", "cpp-shmap"]` — exactly the three metrics asked about, for both tools.

3. **The current result set already has the data**, checked directly against
   `benchmarks/results/suite-1.0/current/results.tsv` rather than assumed from the config: all six
   `(impl, metric)` combinations — `shmap-rs`/`cpp-shmap` × `Containment`/`Jaccard`/`bucket_SH` —
   are present, each across all five benchmarks (B01–B05). This is what RESULTS.md §2 ("Versus the
   C++") already reports.

**One piece of dead code found while checking, not part of the answer.** The repo also carries a
legacy `Makefile` `eval_*` pipeline, ported unchanged from the original C++ repo's own Makefile
(see the file's header comment) and superseded by `benchmarks/run.py`/`suite.toml` for anything
this project documents or gates on. Its `eval_shmap_on_datasets_on_metrics` target sweeps
`METRIC=bucket_SH`, `bucket_LCS`, then `fixed_C` — `fixed_C` is not a value either binary's `-m`
accepts (confirmed: both reject it with a clear error), and the target never touches `Jaccard` at
all. Not fixed here — it's inherited, unused by the documented benchmark flow, and out of scope for
"is this already satisfied"; flagged for Pesho in case it's still expected to work for something.

**Outcome.** No `src/`, `suite.toml`, or `run.py` change. This PR documents the verification in
`QUESTIONS.md` only.

---

## Q3 — Speed and memory vs the C++/paper, verified against real C++ source with code snippets

**Asked** 2026-08-02 · **Branch** `q3-optimizations-list` (merged) · **PR** #8 · **Status** merged 2026-08-02

**Question, as first asked.** *„тук ни трябва много подробно обяснение на това какво е сменено с
всички базови дефиниции, как промените от оригиналния C++ shmap и статията са имплементирани,
какво се променя като изчисление и структура от данни и защо. Подредено по значимост. да направи
списък от доп. оптимизации по сравнение с оригиланата статия за shmap"* — a very detailed
explanation of what changed, with definitions, how each change is implemented relative to the
C++ and the paper, what changes computationally and in data structures, and why — ordered by
significance.

**Redirect.** The first draft of `PORT_CHANGES.md` covered every category of change (correctness
fixes, the multithreading architecture, new CLI capabilities, and deliberately-kept quirks,
alongside performance). Pesho clarified the actual scope: specifically the changes that improve
single-thread speed and reduce memory usage, with deeper motivation than the first pass gave them.
`PORT_CHANGES.md` was rewritten around that scope rather than kept as the broader survey — the
earlier, broader version is still visible in this branch's git history if the fuller picture is
ever wanted.

**Answer.** [`PORT_CHANGES.md`](PORT_CHANGES.md), now 8 numbered sections matching Pesho's own
list, each with real C++ and Rust code. Memory is almost entirely one data structure's
three-generation evolution; multithreading and parallel indexing (restored per this round's ask)
are genuinely new capabilities absent from the C++; the rest — streaming multi-hit seeds,
`match_rest` memoization, the two-pass reader, sketching hot-loop work, and allocation/memory-
traffic reductions — are roughly ten individually-measured techniques that compound. Ties back to
both RESULTS.md's current headline figures (1.91–2.74x single-threaded speed, 6.90–7.43x less
peak memory) and the depth-measurement figures Pesho cited (1.89–2.04x, 8.2–9.6x), explaining why
both are real and where they differ. Still out of scope: correctness fixes and new CLI features,
noted at the end with a pointer to where they live.

**Headline findings:**

- **Memory is almost entirely one story.** The bucket accumulator (`Buckets`) went from a
  per-segment dense array sized by the *reference* (~15 GB per worker thread) through an
  intermediate hashmap and radix-sort design, to the current array sized by the *read's own*
  half-length (~4 MB, L3-resident) — three to four orders of magnitude smaller, because it scales
  with how finely one read partitions the reference rather than with the coarsest partition the
  algorithm ever allows. Generation 1's huge allocation was also a genuinely counterintuitive
  *speed* bug: multithreaded whole-genome runs sometimes got slower with more threads, because a
  worker finishing that allocation last started with zero reads left.
- **One optimization is directly prescribed by the paper, not invented by the port.** The paper's
  own Algorithm 4 names "Optimization 2: sort blocks by decreasing number of matches," reasoning
  that it makes the acceptance threshold rise earlier. The C++ implements this with `std::sort`
  (not stable); shmap-rs implements the same paper-prescribed ordering by sorting a packed 64-bit
  key instead of the full record — reproducing stable-sort semantics with an unstable, faster sort,
  and as a side effect becoming deterministic where the C++ isn't.
- **Speed is an accumulation, not one dominant change** — `RefineCache`'s memoization of the
  second-best search (44% of a hot function's calls eliminated, restricted to exactly the two
  metrics where it's provably safe), the rolling-hash rotation tables plus bounds-check removal,
  the two-pass parallel reference reader, mimalloc, and roughly six smaller techniques each worth
  single-digit percentages.

**Relationship to existing docs.** [`PROFILING.md`](PROFILING.md) already tracks every
optimization chronologically with exact before/after numbers at the time each landed —
`PORT_CHANGES.md` cites those rather than re-deriving them, reorganized around *why* each change
exists rather than *when* it landed, and framed specifically against the paper and the C++.

**Methodology.** Every `src/*.rs:line` citation was checked against the file on disk after
writing (one was wrong on the first pass — a counter-reset citation that pointed at the wrong
function — found and fixed before this was pushed). Every quoted number was traced to its source:
either a `PROFILING.md`/`RESULTS.md` figure or a doc comment already in the source, confirmed by
the port's own stated practice of grep-based call-site audits rather than assumption.

**Second expansion.** Pesho supplied a specific 8-item list of optimization claims (adaptive
bucket accumulation, streaming multi-hit seeds, `match_rest` memoization, multithreaded mapping,
parallel indexing, two-pass FASTA parsing, sketching hot-loop work, and allocation/memory-traffic
reductions), most with direct GitHub links into `pesho-ivanov/shmap` at commit `63f1103`, and
asked for each to be verified and expanded with exact data-structure names and code snippets —
explicitly asking to keep the memory/speed findings already written, and to restore the
multithreading and indexing sections the previous redirect had explicitly moved out of scope.

This time the actual C++ source was fetched — `curl` against raw.githubusercontent.com works from
this session even though the C++ source isn't checked out anywhere on `a2` — rather than relying
only on what the Rust doc comments say about it, and every C++ snippet in `PORT_CHANGES.md` now
quotes that fetch verbatim with exact line numbers rather than paraphrasing a port-time comment.
This surfaced one correction worth recording: `RefSegment::seq`'s own comment in the C++ says
"empty if only mapping and no alignment," but the constructor call that actually builds the index
(`index.h:104`) passes the real sequence unconditionally — so the field holds a full second copy
of the genome on *every* run, not conditionally as the comment implies. The claim about it
("dead code upstream") holds and is now stronger, verified against two fully-commented-out call
sites rather than taken on the Rust side's word for it.

One more nuance found while verifying the `diff_hist`-is-a-dense-vector claim: there are two
`diff_hist`s in the codebase. The hot path (`match_rest`/`find_best_mapping`, every normal read)
uses the dense `Vec<QPos>` the claim describes; a second, hashmap-keyed one exists in `refine.rs`'s
`Matcher`, but backs only the optional ground-truth diagnostic path in `analyse_simulated.rs` —
off by default, not part of normal mapping. Recorded so the claim is precise about which path it
describes.

`PORT_CHANGES.md` is now organized around the 8 claims directly (renumbered 1–8 to match how they
were presented) rather than the prior Memory/Speed split, since that's the structure that was
asked for; each section states the concept, then the C++ implementation with a real snippet, then
the Rust implementation with a real snippet, then the measured effect.

**Outcome.** New file, `PORT_CHANGES.md`, plus a pointer added to `README.md`'s documentation
table. No `src/` change.

**Third redirect, 2026-08-03.** *"historical data is not needed — Pesho wants to compare current
state with his paper and C++ shmap, and wants the changes in a table."* `PORT_CHANGES.md` led with
narrative — each optimization as a small essay, and §1 (bucket accumulation) specifically told the
story as three generations of attempts rather than stating the current design plainly. Both
restructured, nothing deleted:

- **A table now opens the document**, immediately after the one-paragraph summary: one row per
  optimization, columns for what it optimizes, its measured effect, and whether it's exact (byte-
  identical output) or not — every optimization here *is* exact, which the table now states
  per-row rather than leaving implicit. Rows without an isolated standalone measurement say so
  plainly instead of estimating one.
- **§1's "three generations" narrative moved to a clearly separated, explicitly-optional closing
  subsection** ("How this design was reached"); the current design's own description now leads,
  stated directly rather than as the endpoint of a story. `PROFILING.md` remains the
  never-updated, fully chronological record for anyone who wants the complete history — that was
  already its job, and still is.
- **Fixed a dangling cross-reference found while restructuring**, unrelated to the redirect but
  caught in the same pass: §1's C++ description promised "§3 below covers this sort specifically"
  for the paper's Algorithm 4 Optimization 2 (the final descending-match-count sort) — but no such
  section existed; §3 is `RefineCache`, a different optimization entirely. This was never actually
  documented with its own C++/Rust snippet pair despite being the one optimization the paper names
  directly. Now it is, in place, right where the C++ snippet already was — plus one detail the
  fix surfaced: shmap-rs's version is not just as-good but *more* deterministic than the C++'s,
  since it reproduces exactly what a stable sort would give (`sort_unstable` on a tiebreak-packed
  key) where the C++'s own `std::sort` gives no such guarantee even between its own runs.

Applies going forward too: new optimizations should add a row to the table above, not just a
prose subsection.

---

## Q4 — Thread scaling caps at ~7x on 64 threads: diagnose, and is rayon the fix?

**Asked** 2026-08-02 · **Branch** `q4-thread-scaling` · **PR** #9 · **Status** in review

**Question.** *"scaling with threads doesn't look good. 7x for 64 threads. My colleague dpetrov
suggests rayon library. In any case, scaling with threads needs real significant optimization"* —
diagnose the poor thread scaling and fix it; a colleague suggested switching to `rayon`.

**Answer.** The premise is right — B01/B02 land at 6-8x at `-@64` — but the cause isn't the
threading library or the pipeline design. It's memory-bandwidth contention on the shared reference
index, which starts as soon as a second worker joins (real, measurable per-read cost inflation
from just `-@2` on) and accelerates sharply once `-@` needs a second of `a2`'s 4 sockets (16 cores
each, NUMA). `rayon` would not fix this: its default thread pool has no NUMA awareness, and its
work-stealing scheduler could plausibly make locality *worse*, not better. Full diagnosis and the
follow-up plan are now in [RESULTS.md §3](RESULTS.md#3-thread-scaling) and
[§11](RESULTS.md#11-what-to-try-next).

**Correction, same day.** The first pass of this answer reported worker efficiency as "~92% flat
through 16 threads, then a cliff at 32/64" — Pesho asked directly whether that was really right,
since whole-run wall time already looked well behind ideal scaling before 16 threads, and the
answer was no: that 92% was `cpu_query_mapping / (threads x wall_mapping)`, which measures whether
threads are *busy*, not whether each read costs what it should. It doesn't stay flat because it
is answering the wrong question — a thread can be 100% busy while doing 28% more work than
necessary per read, which is exactly what happens here starting at `-@2`. The corrected metric
(step 2 below) and RESULTS.md §3 now report per-read CPU cost against its `-@1` value instead,
which shows the true, continuous shape: real degradation from the second thread on, accelerating
2-4x steeper the moment a second socket is needed. The `numactl` evidence, the AVX-512 rule-out,
and the "not `rayon`" conclusion are unchanged by this correction — only the shape of the
degradation curve was wrong, not its cause.

**How this was diagnosed, in order:**

1. **Ruled out the pipeline first, with instrumentation already in the codebase.** The collector's
   `max_pending_reorder_buffer` counter (recorded per run, previously unused for this) grows from
   1 to 1,942 between `-@1` and `-@64` on B01 — more out-of-order completion at higher parallelism,
   exactly as expected — but `collector_busy` stays flat at 1.6-2.1 s across every thread count, so
   the collector isn't struggling with it. The reader's own `query_reading` timer doesn't grow
   either. Neither is the bottleneck.

2. **Found the real signal in per-read CPU cost, not busy-time.** `cpu_query_mapping / mapped_reads`
   against its `-@1` value — computable from `profiles.tsv`, built for the paper artifacts in an
   earlier question — rises continuously from `-@2` on: +4-18% at `-@2`, +25-45% by `-@16`, then
   +39-138% at `-@32` and +76-240% at `-@64`, on *every* benchmark. (The first pass of this
   investigation instead divided by `threads x wall_mapping`, which measures whether threads are
   busy rather than whether work is inflated, and looked flat through 16 threads as a result — see
   the correction above.) `lscpu`/`numactl --hardware` showed why the acceleration is sharpest at
   32 and 64 specifically: those are exactly 2 and 4 sockets on this host, against 1 for 16.

3. **Confirmed with direct `numactl` experiments**, host verified idle first (load average ~1 on
   64 cores). `numactl --membind=0` (forcing all memory onto socket 0, worst case for the 48
   workers on the other three) makes `-@64` *slower* than the unconstrained run — the natural
   placement already beats that floor. `numactl --cpunodebind=0 --membind=0 -@16` (16 threads
   confined to one socket) matches or beats an unconstrained `-@64` on the same dataset — sixteen
   well-placed threads outrunning sixty-four poorly-placed ones. `--interleave=all` did *not* help
   (slightly worse than natural at 32 and 64), which rules out simple single-node hotspotting as
   the whole story — it's aggregate cross-socket traffic once enough workers generate it at once,
   not a placement problem fixable by moving the index to one "right" node.

4. **Checked and ruled out AVX-512 downclocking** — raised mid-investigation: these CPUs are
   Cascade Lake, which throttles package-wide under sustained multi-core AVX-512 use, and that
   *would* produce a many-cores-busy regression that looked like this. `objdump` on
   `target/release/shmap` finds zero AVX-512 instructions — nothing in the build sets
   `target-cpu=native`, so rustc emits the generic x86-64 baseline. Not the cause here, but now
   recorded as project guidance for future SIMD work on this host (it *was* a live risk in Q1's
   probes, which explicitly used `target-cpu=native`).

**On rayon, directly.** Not recommended for this problem. `rayon::ThreadPoolBuilder`'s default
global pool has no concept of NUMA nodes — it doesn't replicate the shared index per socket or pin
workers to a memory domain, so porting the existing per-read parallelism to `rayon::par_iter`
would leave the measured bottleneck exactly where it is. Work-stealing is a genuine risk of making
it *worse*: a stolen task can run on any core in the pool, including a different socket than
wherever its data was last touched, which is the opposite of what a NUMA-bound workload needs. If
`rayon` simplifies the current hand-rolled channel/`thread::scope` pipeline later, that's a
maintainability argument on its own merits — independent of, and not a substitute for, fixing the
memory placement.

**The real fix, scoped but not built.** A copy of the sharded index per NUMA node, each worker
reading its own node's copy instead of one shared copy every socket reaches across the
interconnect for. Cost: `N_nodes x` the current index size — ~10 GB on this 4-socket host at
today's ~2.5 GB single-copy size, still well under the C++'s constant 18.85 GB. Not attempted in
this PR: it needs runtime NUMA topology detection and node-bound allocation (neither in `std`, so
a new dependency), and testing across topologies this one remote host can't provide alone — a
single- or 2-socket host needs to confirm it before it ships, or the common case regresses to
carry a benefit only multi-socket hosts see.

**Usable today, no code change.** On this host, capping `-@` at 16 (one socket) or running under
`numactl --cpunodebind=0 --membind=0` outperforms an unconstrained `-@64` — immediately actionable,
zero risk, stated in RESULTS.md §11.

**Outcome.** `RESULTS.md` §3 gained the full diagnosis with the efficiency table and both
`numactl` experiments; §11 gained the scoped NUMA-replication follow-up and the rayon rule-out;
the host provenance line now states the socket topology instead of just "64-core". No `src/`
change — the fix is real engineering that shouldn't be rushed into the PR that diagnosed the
problem.

---

## Q5 — Build the NUMA-aware index replication Q4 scoped but didn't ship

**Asked** 2026-08-03, follow-up directive after Q4 · **Branch** `fix-numa-index-replication`
(not merged) · **PR** #12 (closed, not merged) · **Status** dropped

**Question.** *"Now fix the problem with the poor threading performance"* — build the fix Q4
diagnosed and scoped but explicitly didn't attempt: the memory-bandwidth contention on the shared
reference index that inflates per-read CPU cost continuously from `-@2` on, sharply once a run
needs a second socket.

**Answer.** Built, measured, and dropped: five escalating, independently-implemented and
independently-measured versions of "one copy of the index per NUMA node" all net-regressed against
doing nothing, on this host, at every thread count that touches more than one socket. Full account
in [RESULTS.md §11](RESULTS.md#11-what-to-try-next); summary here.

1. **Even split across every available node.** `-@16` (fits on one 16-core socket) still built
   four replicas. `-@64` ended up slower than `-@32`.
2. **Packed into the fewest nodes that fit**, but each replica built by one single thread calling
   plain `.clone()`. Fixed (1), re-introduced close to the ~7.7 s serial hash-map-insert floor
   sharding the index's own build exists to avoid — once per node.
3. **Parallelised the clone** one thread per shard and per segment. Faster, still net-negative.
4. **`set_mempolicy`/`MPOL_BIND`, then `mbind`/`MPOL_MF_MOVE`.** Root-caused why (2)/(3) still
   failed: `/proc/sys/kernel/numa_balancing` reads `1` on this host, so the kernel's own automatic
   page migration overrides plain CPU-pinning-plus-first-touch regardless of allocation size.
   Explicit `mbind` placement verified correct via `/proc/<pid>/numa_maps` — still net-negative,
   because actively migrating an already-misplaced buffer costs a real copy proportional to how
   wrong the placement was.
5. **Bypass the allocator entirely.** mimalloc (the global allocator) eagerly commits and reuses
   large arena pages an unrelated, unpinned thread already touched — usually one of `build_index`'s
   own workers — defeating even a correctly pinned-and-bound thread's "fresh" allocation. This
   version (`src/numa.rs`, `src/numa_storage.rs`) routes replica storage straight through `mmap(2)`
   — a hand-rolled `NumaBuffer` plus an open-addressing `RawHashTable` standing in for `HashMap`
   (there is no safe way to hand `HashMap` a caller-owned backing buffer), with the binding
   thread's memory policy set *before* the allocating call. This is the version that actually
   achieves correct placement, confirmed via the same `numa_maps` trace — and it is *still* a wash
   at best (`-@16`, single-node, roughly ties the unreplicated baseline) and a clear loss wherever
   a second socket is needed, including B04 (the mapping-dominated dataset, where this fix should
   matter most) at `-@64`: 44.5 s replicated vs. 33.7 s unreplicated.

Every version kept output byte-identical to `-@1` (`strip_time_tag`-diffed, matching `run.py`'s own
`thread_determinism` check) and passed the full test suite at every stage — none of this was ever a
correctness question. It's an economics one: on this host, replicating a ~2.5 GB/socket index costs
more wall-clock time (mmap, page-fault, first-touch) than the cross-socket memory traffic it avoids
ever costs during actual mapping, even on the longest mapping phase measured (B04 `-@64`,
~30-45 s total). A workload with a much longer mapping phase relative to index size could plausibly
see this pay off, but that's a different, unmeasured regime — not grounds to carry the extra
complexity (two new modules, one genuinely `unsafe` — `libc::mmap`/`munmap`/`syscall`, isolated and
each call individually justified with a `SAFETY:` comment, but real unsafe code in a crate that
previously had none) for a change that loses on every workload actually measured.

**On `rayon`, unchanged from Q4.** Still not the fix — no NUMA awareness in its default pool,
independent of anything found here.

**Outcome.** PR #12 closed without merging. `src/numa.rs` and `src/numa_storage.rs`, the
`Shard`/`KmerStorage` storage-enum changes, `--no-numa`, and the `libc`/`core_affinity`
dependencies are reverted off `main` — none of it ships. RESULTS.md §11 and this entry are the
lasting record, so the next person who has this idea starts from "tried, five ways, here's why
each failed" instead of from zero. Q4's `numactl --cpunodebind=0 --membind=0` (or capping `-@` at
one socket) remains the actionable answer for multi-socket hosts.

---

## Q6 — Indexing time vs mapping time, per tool, for third-party comparisons

**Asked** 2026-08-03 · **Branch** `q6-index-vs-mapping-time` · **PR** #14 · **Status** in review

**Question.** *"we need indexing time for each tool separately from mapping time (they combine to
total time). The idea of Pesho how to measure it for each third-party tool we are comparing shmap
and shmap-rs with is: run each tool twice — once for measuring the indexing time by mapping only 1
read, and once for measuring the whole time (what you currently have). To get the mapping time,
subtract the [index-only run's time] from the time for [the full run]."*

shmap-rs already reports this split via its own `-x`/`--profile-log` instrumentation (wall timers
around the `indexing`/`mapping` brackets — see `RESULTS.md` §3b). The C++ reference emits no such
report, so `run.py`'s `index_s`/`map_s` columns were empty for every `cpp-shmap` row, and §3b's
table was subject-only.

**Answer / change.** Implemented Pesho's two-run method as the general fallback for any
implementation without a native phase report:

- **[`benchmarks/run.py`](benchmarks/run.py)** — `measure()`'s existing branch (native JSON report,
  for `shmap-rs`) gets a new `else`: for any impl without one, a second invocation runs the
  identical command with the reads file swapped for `one_read_fasta()`'s cached one-record slice.
  Indexing doesn't depend on the read set, so that run's wall time is (almost) entirely indexing;
  `map_s` is recovered as `wall_s - index_wall`. The sub-run happens inside `measure()`, once per
  call, so it inherits the same repeat-and-median treatment `[run.reference_impl]` already gives
  every other number for the C++ — no new aggregation logic needed.
- `one_read_fasta()` streams just past the first record's boundary rather than reading the whole
  file (real reads files are up to tens of GB), and caches the slice per result set so repeated
  calls for the same dataset don't re-derive it.
- **[`benchmarks/report.py`](benchmarks/report.py)** — §3b's `block_phase_split` table gains an
  `impl` column and now includes the reference impl's row(s) alongside the subject's, so the two
  are directly comparable in one place instead of subject-only.
- **[`benchmarks/test_run.py`](benchmarks/test_run.py)** (new) — `one_read_fasta` is the one piece
  of this that's pure and easy to get subtly wrong (truncate mid-sequence, grab the wrong record
  count) without it being obvious from a single manual check; unit-tested directly, wired into CI.

**Verified.** Ran the real two-run method by hand against `cpp-shmap` on B01 (149,194 reads):
index-only run 32.65 s, full run 107.72 s, derived mapping time 75.07 s — the index-only PAF has
exactly one line, confirming the read-set swap. `report.py --check`/`paper.py --check` regenerate
clean against the existing (subject-only, pre-this-change) cached result set, since no `cpp-shmap`
row has `index_s`/`map_s` populated yet — the new reference-impl branch of the table is additive
and simply has nothing to show until the next C++ re-measurement. All cheap-tier checks
(`cargo fmt`/`clippy`/`test` release+debug, `validate_suite.py`, `test_compare.py`,
`test_concordance.py`, the new `test_run.py`, `report.py --check`, `paper.py --check`) pass.

**Scope note.** This covers `cpp-shmap`, the only third-party tool currently measured for timed
comparison in `run.py`/`suite.toml`. `reference_mappers.py`'s corpus (mapquik, Winnowmap2,
minimap2, blend) is a separate, accuracy/concordance-only pipeline that doesn't measure wall time
for PR comparisons at all — extending index/mapping timing there would be a different, larger
change, not attempted here.

**Outcome.** Pending the benchmark verdict. No `src/` change — `run.py`/`report.py` only affect
future re-measurement, not the binary.

---

## Q7 — Optimize the refinement step (it differs for Containment/Jaccard)

**Asked** 2026-08-03 · **Branch** `q7-refinement-jaccard-bound` · **PR** #16 · **Status** in
review

**Question.** *"try to optimize the refinement step. it is different for C and J"* — Containment
and Jaccard cost noticeably different amounts of wall time in `refine`/`match_rest`, and the ask
was to try to speed it up.

**Answer.** Measured first, then designed, then concluded no safe change exists to make — a real
negative result, not an unattempted one. Full mathematical account in
[RESULTS.md §11](RESULTS.md#11-what-to-try-next) ("A tighter Jaccard pruning bound — investigated,
ruled out mathematically"); summary here.

**What's actually different.** Profiled `refine` on B01 at `-@1`: Containment 6.27 s / 620,342
refined buckets vs Jaccard 16.43 s / 1,676,532 refined buckets — a 2.62x time ratio against a
2.70x bucket-count ratio. The per-bucket cost of refining is essentially metric-symmetric; the
entire gap is that Jaccard's pruning pass lets more buckets through before they ever reach
`refine`, not that refining costs more per bucket under Jaccard.

**Why the obvious fix — tighten pruning's bound (`hseed`) specifically for Jaccard — isn't safely
possible from what pruning cheaply tracks today.** `hseed` bounds `matches/m`, exactly
Containment's own score formula. Jaccard's ceiling for a bucket — the best case where its eventual
scoring window contains zero non-matching filler k-mers — collapses to that exact same value, so
`hseed` is already the *tightest bound obtainable* from aggregate counts, not a loose stand-in.
Checked whether the `r_min`/`r_max` extremes pruning already tracks per bucket (for free, via
`matches_in_bucket`) would tighten it further: they don't, because the adversarial case pruning
must not wrongly rule out — "this bucket is about to miss one match, but the rest are packed
arbitrarily tight nearby" — can't be excluded by a count and two extremes. A genuinely tighter
bound needs the actual sorted match positions within the bucket, at which point pruning stops
being a cheap filter ahead of `refine` and starts doing comparable work to `refine` itself.

**Also checked and ruled out:** a per-call constant-factor speedup of `best_fixed_length` itself
(the shared sweep both metrics use). The metric dispatch inside it (`mapping_score`'s match on
`Metric`) runs once per outer-loop position, not once per k-mer swept, so it is not a meaningful
cost regardless of metric. No lower-risk structural win was found in the sweep itself either;
`seed_heuristic_pass` — the pruning loop right upstream of it, and the more natural place any
metric-aware tightening would have to live anyway — already carries its own doc comment
documenting that bundling its state into a context struct was tried and measured as a net loss
twice, on the same memory-latency-bound reasoning (`src/shmap/pruning.rs`); that specific finding
belongs to pruning, not to `best_fixed_length`, but the same caution applies to restructuring
either of these two loops without a live measurement in hand.

**Outcome.** No `src/` change. The C/J refinement cost gap is an inherent property of the
algorithm — Jaccard's theoretical ceiling for a bucket genuinely can equal Containment's — not an
implementation gap. A real fix would mean tracking match-position lists per bucket during pruning
and re-measuring whether the bookkeeping cost is smaller than the buckets it would save: a new,
real investigation with its own cost/benefit measurement, not attempted here given the correctness
stakes of a wrong pruning bound (the same class of risk `-M`/`--max_matches` was rejected for,
RESULTS.md §8).

---

## Q8 — Can SIMD be used in some of the steps of mapping a read?

**Asked** 2026-08-03 · **Branch** `q8-simd-mapping-steps` · **PR** #17 · **Status** in review

**Question.** *"can SIMD be used in some of the steps of mapping a read?"*

**Answer.** Investigated across every per-read mapping step, not just sketching (which Q1 already
answered: SIMD k-mer emission measured 0.79-0.96x, a wash to a regression, before even accounting
for this host's AVX-512 downclocking). Full account in
[RESULTS.md §11](RESULTS.md#11-what-to-try-next) ("SIMD in the per-read mapping steps —
investigated, doesn't fit the dominant costs"); summary here.

**What's actually dominant.** Profiled fresh at the standard benchmark parameters (`k=25`, `-@1`,
B01) rather than reusing older k=15 whole-genome figures, since the cost balance differs at the
paper's own operating point: `match_rest`/`refine` is the single largest per-read cost at 31.3% of
`mapping`, ahead of `match_seeds` at 21.1% and sketching at 19.3%.

**Why neither of the two largest fits SIMD.** `match_seeds` streams pre-sorted hits through a
two-slot accumulator (Q3/PORT_CHANGES §2) — an O(1)-per-hit design chosen specifically to replace
a hashmap, where each hit's processing depends on state carried from the previous one; vectorizing
across hits means giving up the streaming property that redesign exists for. `match_rest`/`refine`
(`best_fixed_length`) sweeps reference k-mers doing a hashtable lookup and a data-dependent array
index per element — memory-latency-bound, the same shape already documented for the neighboring
pruning loop (Q7), where restructuring was tried and measured as a net loss twice. The genuinely
uniform arithmetic that does exist on this path (`DenseSlot::add`) is cheap enough relative to the
random memory access around it that a perfect vectorization would be unmeasurable, and the struct
is deliberately kept at 16 bytes for L3 residency — widening it for SIMD lanes works against that.

**One real, modest candidate, not pursued.** `unique_elements_with_info` sorts each read's own
k-mers by hash (~4-5% of `mapping` combined). A packed-key sort — the same technique already used
elsewhere in this codebase for `get_sorted_buckets`'s final ordering — could plausibly speed this
up, but the ceiling is small (a few percent of `mapping` at best) and reproducing the exact
tie-break semantics correctly needs real care for a reward this size.

**Outcome.** No `src/` change. SIMD is not a strong lever for the current dominant per-read mapping
costs — they are memory-latency-bound and hashtable/pointer-chasing-heavy, not the regime SIMD
helps with. Documented rather than attempted, matching how Q4/Q7 handled findings of this shape.

---

## Q9 — Software prefetching for the pruning lookups

**Asked** 2026-08-03, follow-up to Q8 · **Branch** `q9-prefetch-refine` · **PR** #18 · **Status**
in review

**Question.** Following Q8's finding that SIMD doesn't fit the dominant per-read costs because
they're memory-latency-bound: does software prefetching — the technique that actually targets a
latency-bound loop — help instead?

**Answer.** Probed before touching any real code, and measured negative. Full account in
[RESULTS.md §11](RESULTS.md#11-what-to-try-next) ("Software prefetching for the pruning lookups —
probed, measured negative"); summary here.

**Refined the target first.** `best_fixed_length` (Q7/Q8's focus) mostly touches `p_ht`/`diff_hist`
— small, per-read, cache-resident structures by the time they matter. The function that actually
does random access into the multi-GB reference index is `matches_in_bucket` (called from
`seed_heuristic_pass`, during pruning) via `tidx.single_hit`/`multi_hits` — and it's the one whose
own doc comment already calls the workload memory-latency bound. That's the correct target for a
latency-hiding technique, not `best_fixed_length`.

**Probed, not implemented.** `profiling/prefetch_probe.rs`: an open-addressing table sized to this
host's real per-shard scale (4M entries, ~100 MB, well past L3), queried at real-run order of
magnitude (20M lookups), three hit rates, lookahead depths 4-32. Three variants against the same
table and query stream: plain sequential baseline, safe-Rust lookahead (reordered but no explicit
hint), and explicit `_mm_prefetch`.

- Plain lookahead (no hardware hint): **consistently 35-40% slower** than baseline — the ring-buffer
  bookkeeping isn't compensated by anything.
- Explicit `_mm_prefetch`, shallow depth (`D=4`): still slower (0.83-0.86x).
- Explicit `_mm_prefetch`, deeper (`D=16-32`): roughly breaks even (0.96-1.05x) — never a clear win.

Baseline itself runs ~25-30 ns/query for a table many times larger than L3 — fast enough that the
CPU's own out-of-order execution is already overlapping independent loads without help, leaving
little latency for explicit prefetching to additionally hide.

**Outcome.** No `src/` change — the probe answered the question before any real code needed to
change, so there is nothing to benchmark against the official suite. The hypothesis was reasonable
(this loop is genuinely memory-latency bound, confirmed independently by its own doc comment) but
doesn't survive contact with a real measurement: modern out-of-order execution already does most of
what explicit prefetching would add, for this access pattern and table size, on this host.

---

## Q10 — 2-bit-packed sequence encoding for sketching

**Asked** 2026-08-09, follow-up after Q9 · **Branch** `q10-2bit-packed-sketching` · **PR** #20 ·
**Status** in review

**Question.** After Q9, asked whether the negative results so far meant nothing else could be
optimized. They don't — they were four specific hypotheses, not an exhaustive search. Of the
candidates still genuinely open, 2-bit-packed sequence encoding was the one worth doing next:
[`PROFILING.md`](PROFILING.md)'s own "remaining bottlenecks" names it as one of two levers left for
sketching (~2.0 ns/base, 19.3% of `mapping` plus a large share of indexing), it had never been
attempted, and it is a *different mechanism* from SIMD — so Q1/Q8's negative SIMD results did not
rule it out.

**Answer.** Probed before touching real code, and measured negative — but the probe also identified
what actually limits this loop, which is the more useful result. Full account in
[RESULTS.md §11](RESULTS.md#11-what-to-try-next); summary here.

**The packing measurement** (`profiling/pack2bit_probe.rs`, 50 Mbase, `k=25`, all variants asserted
to produce bit-identical accumulators):

| variant | ns/base | vs. baseline |
|---|---:|---|
| baseline (byte-per-base, the real loop) | 1.013 | — |
| 2-bit packed, naive | 2.058 | 0.49x — 2x slower |
| 2-bit packed, unrolled x4 (its best case) | 1.131 | 0.90x — 10% slower |

Both forms were tested deliberately: measuring only the naive one would have strawmanned the idea.
It loses even unrolled, with one `u64` load per stream per four bases and no per-base branch.

**Why — and a mid-investigation correction.** Baseline works out to ~3.95 cycles/base at this
host's 3.9 GHz single-core turbo (measured in Q1's addendum above), which is close to the length of
the serial `rotate -> xor -> xor` chain, so I suspected the loop was *dependency*-bound — which
would have meant Q1's stated premise ("the lever is removing loads, not adding independent chains")
was wrong. Tested it directly rather than assuming (`profiling/chain_probe.rs`, 1-3 independent
hash chains over disjoint slices): **three independent chains buy only 1.11x**. A dependency-bound
loop would have nearly multiplied. So the suspicion was wrong and **Q1's premise was right** — now
confirmed independently, from the opposite direction.

What binds instead is load-port throughput: 6 loads/base (two sequence bytes, four LUT entries)
against two load ports is a hard 3 cycles/base, against ~3.6-4.0 measured; ALU isn't close (~8
ops/base over four ports). That explains the packing result exactly — packing removes 1.5
loads/base, but the *cheapest* ones (sequential, prefetcher-friendly), and pays for them in
shift/mask ALU work, trading a load-port bottleneck for an ALU one at about the same cycle count.

**Outcome.** No `src/` change, so nothing to run the benchmark suite against. The sharpened finding
is more useful than the negative one: the four **LUT** loads are this loop's real cost, not the two
sequence loads — so 2-bit packing was aimed at the wrong half. The one lever that does target them
is replacing table lookups with register permutes, which is precisely Q1's AVX-512 attempt: it won
on hashing alone and lost only once real k-mer emission was included. Sketching's rolling loop is
therefore close to its floor on this hardware, and 2-bit packing's remaining merit is memory
footprint rather than speed — largely moot here, since this port already discards the reference
sequence after sketching ([`PORT_CHANGES.md`](PORT_CHANGES.md) §8).

---

## Template

Copy this for each new question.

```
## Q<n> — <short title>

**Asked** <date>, <where: email / issue / call>
**Branch** `q<n>-<slug>` · **PR** #<n> · **Status** <status>

**Question.** What Pesho actually asked, in his terms.

**Answer / change.** What we did or concluded. Link the section of RESULTS.md that carries the
evidence rather than repeating numbers here.

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

This is not cosmetic: [RESULTS.md §8](RESULTS.md) shows accuracy is bounded by the sampling rate,
so which set is authoritative changes what every table in the file is a measurement *of*.

## P2 — `lost_on_seeding` / `lost_on_pruning` report nothing

Both are inert in the C++ revision this port was made from: `lost_on_seeding` is a hardcoded `0`,
and `lost_on_pruning` is threaded through `match_rest` as an out-parameter that nothing writes, so
its caller reports a constant 1 per read. shmap-rs ports both as the same inert bumps
(`src/shmap/scoring.rs`, `src/shmap/mod.rs`), which is why the archived profiles carry
`lost_on_pruning` exactly equal to the read count.

The draft's seed-heuristic table reports small non-zero values for "lost on pruning", so those came
from something other than this code path. Which build produced them? Measuring the quantity
properly needs a run with pruning disabled to diff against — see [RESULTS.md §11](RESULTS.md).

## P3 — Per-read scaling does not cleanly show the logarithmic term

`fig_time_vs_matches` is instrumented and plotted, but the fits do not settle the claim: against
matches examined, a linear fit beats a logarithmic one on B02 (R² 0.958 vs 0.677) and B05 (0.949 vs
0.763), while B03 goes the other way. That is *not* evidence against `O(R·m·log M)` — the bound's
three per-read factors co-vary, so a single-variable fit misattributes their combined growth.
Isolating the log term needs reads matched on `R` and `m`. Worth agreeing on the right experiment
before the claim appears in print.
