# Questions and change requests from Pesho

The running log of what was asked, what was done about it, and what the benchmark said. One
question at a time; one branch and one PR each, so a verdict is attributable to a single change.

Process is in [CONTRIBUTING.md §0](CONTRIBUTING.md). Keep entries short — the reasoning belongs in
[RESULTS.md](RESULTS.md) or the commit message, and this file is the index.

| # | Question | Branch | PR | Status |
|---|---|---|---|---|
| Q1 | Replace `sketch.rs` with an already-optimised library | `q1-sketch-library` | — | in review |

Status is one of: **open** (not started) · **in progress** (branch exists) · **in review** (PR open,
awaiting the benchmark) · **merged** · **dropped** (with the reason in its section).

---

## Q1 — Replace `sketch.rs` with an already-optimised library

**Asked** 2026-08-02 · **Branch** `q1-sketch-library` · **Status** in review

**Question.** *„sketch.rs да се замени от библиотека, която вече е оптимизирана."* — replace our
sketching code with an existing, already-optimised library instead of maintaining our own.

**Answer.** Keep `sketch.rs`. The library exists and the hash is genuinely standard, but swapping it
in would change every mapping *and* run 4.5x slower — and writing SIMD ourselves buys 1.33x on
sketching, which is ~4% end to end and below the noise floor.

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

### Then we tried SIMD ourselves

The host has AVX-512, so the instruction SimdMinimizers lacked (`vpermq`, a table lookup in a
register) is available. Four variants, all reproducing the scalar checksum exactly, hashing only,
k = 25, 50 Mbase — [`profiling/sketch_simd_probe.rs`](profiling/sketch_simd_probe.rs) and
[`profiling/sketch_lanes_probe.rs`](profiling/sketch_lanes_probe.rs):

| variant | throughput |
|---|---:|
| scalar, as shipped | 727 Mbase/s |
| multi-lane scalar (L = 2…16) | 382–645 Mbase/s — **all slower** |
| AVX-512, 8 lanes, gathered | 427 Mbase/s |
| AVX-512, 8 lanes, transposed input | **3011** hashing only; 587 with the transpose; 441 with the ASCII→code pass too |
| AVX-512, 8 lanes, eight scalar loads, no prep | **969 Mbase/s** |

**The loop is not latency-bound, it is load-bound.** Six L1 loads per base — two sequence bytes and
four table entries. That is why adding independent lanes makes it *slower*: more lanes means
proportionally more loads, and there was no stalled dependency chain to fill.

**The vector core is 4.1x faster, and feeding it is the whole problem.** With the input already
transposed, AVX-512 hashes at 3011 Mbase/s. But a gather costs more than it saves (427), and a
transpose that removes the gather costs more than the hashing it accelerates (587). The only
arrangement that wins reads ASCII directly with eight ordinary scalar loads: **969 Mbase/s, 1.33x**.

**1.33x on sketching is ~4% end to end**, since §5 of [RESULTS.md](RESULTS.md) puts sketching at
15–38% of `query_mapping` — under the ~10% per-row noise floor §10 documents. That does not pay for
unsafe intrinsics in the hot path of a mapper whose output must stay byte-identical, plus runtime
feature detection, a scalar fallback, tail handling and per-lane emission ordering. **Not shipped.**

**The one direction that would pay** is making the transposed layout free rather than paying for
it: the FASTA reader already walks every base, so if it emitted 2-bit codes in lane-major order,
sketching could run near the 3011 Mbase/s core — ~4x. That is a change to the reader and the index
build, not to `sketch.rs`, and it is a separate question.

**Outcome.** No change to `sketch.rs`. The PR adds the test that pins the library finding and the
two probes that pin the SIMD one, so both stay checkable and nobody re-derives them.

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
