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
