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

**Answer.** The library exists and the hash is genuinely standard, but swapping it in would change
every mapping *and* run 4.5x slower. Recommendation: keep `sketch.rs`.

Three findings, each pinned by a test in [`tests/nthash_equivalence.rs`](tests/nthash_equivalence.rs):

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

**Outcome.** No change to `sketch.rs`. The PR adds only the test that documents this, so the claim
stays checkable and nobody re-derives it.

**Follow-up, not done here.** If sketching needs to be faster, the remaining headroom is SIMD: 8
lanes of 64-bit ntHash computed in parallel. No crate offers that with both hashes exposed
(`seq-hash` is SIMD but 32-bit), so it would be our code, not a library — a different question from
this one, and worth asking separately since §5 of [RESULTS.md](RESULTS.md) puts sketching at
15–38% of `query_mapping`.

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
