# profiling/

How to profile a run, the live analysis tools, the standalone probes behind the rejected
optimizations, and the provenance that **cannot be regenerated**.

Current benchmark numbers are not here — they are in [`../RESULTS.md`](../RESULTS.md), generated
from [`../benchmarks/`](../benchmarks/). This directory used to hold ~400 files of shmap-rs
profiling runs; all of it is now produced on demand by `benchmarks/scripts/run.py`, so it was removed rather
than left to rot into a second, contradictory set of numbers.

What survived is what a re-run cannot give back.

---

## Instrumenting a run

`-x`/`--profile-log` (`src/profiling.rs`) writes a per-run JSON report of stage times and per-read
counters. Every benchmark run archives those per result set in
`benchmarks/results/<suite>/<arch>/<set>/raw-profiles.tar.gz`, and
[`../RESULTS.md`](../RESULTS.md) §5 is generated from them — so the stage tables always describe the
commit being measured, rather than whatever was current when someone last edited them by hand.

```sh
python3 benchmarks/scripts/run.py --commit <sha>                       # the maintained runner
target/release/shmap -s ref.fa -p reads.fa -x --profile-log run.json    # one-off
```

**A perf claim needs one binary and a runtime switch, not two builds.** `lto = "fat"` re-lays-out
the whole program on every build, which inflates a change's apparent size 3-6x and invents
regressions in stages the change cannot reach. `SHMAP_NO_REFINE_MEMO`, `SHMAP_DENSE_POLICY`,
`SHMAP_NO_SCORE_SHORTCUT` and `SHMAP_FORCE_P_HT` exist so both arms of a change share a layout. The
measurement behind that rule is in [`../RESULTS.md`](../RESULTS.md) §11.

## Probes

Standalone `.rs` probes, each written to answer one optimization question before touching `src/`.
All of them measured negative; the verdicts and figures are in
[`../RESULTS.md`](../RESULTS.md) §11, and they are kept so the questions are not reopened blind.

| probe | question |
|---|---|
| `sketch_simd_probe.rs`, `sketch_lanes_probe.rs` | can SIMD speed up k-mer emission? |
| `prefetch_probe.rs` | does software prefetching hide the index-lookup latency? |
| `pack2bit_probe.rs` | is 2-bit-packed sequence faster to sketch? |
| `chain_probe.rs` | is the rolling-hash loop dependency-bound or port-bound? |
| `downclock_probe.rs` | how much does AVX-512 downclock this host? |

## Live tools

| file | used by |
|---|---|
| `validate_paf.py` | every benchmark run (`suite.toml` → `checks.validate_paf`), and blocking per [`../VERSIONING.md`](../VERSIONING.md) |
| `adjudicate_disagreements.py` | scores shmap-rs against another mapper *where ground truth exists*, so a disagreement can be attributed instead of guessed at |
| `selective_density.py` | drives the selective-density two-pass of [`../RESULTS.md`](../RESULTS.md) §8 using the stock binary — regions from the first pass's mapq, a dense second pass over those regions only |

```sh
python3 profiling/validate_paf.py out.paf              # structural + score invariants
python3 profiling/validate_paf.py out.paf --truth      # + ground-truth positions
python3 profiling/adjudicate_disagreements.py ours.paf theirs.paf --truth

# selective density, end to end (see the module docstring for the full recipe)
python3 profiling/selective_density.py regions pass1.paf dense.bed
python3 profiling/selective_density.py mini-ref ref.fa dense.bed mini.fa
python3 profiling/selective_density.py select pass1.paf reads.fa ambiguous.fa
python3 profiling/selective_density.py merge pass1.paf pass2.paf merged.paf
```

## other-mappers/ — stored measurements, kept because re-running is prohibitive

Two CSVs of minimap2, Winnowmap2, BLEND, mapquik, minSHmap and the C++ `shmap`, measured once at
older parameters. They are kept because a re-run costs hours per row, not because they are current
— for a maintained comparison use [`../RESULTS.md`](../RESULTS.md) §8. What each file contains, how
to read it, and the two cautions that decide whether a number means what it looks like are in
[`other-mappers/README.md`](other-mappers/README.md).

## datasets/ — how the read sets were made

Provenance for [`../benchmarks/data/datasets.tsv`](../benchmarks/data/datasets.tsv). A dataset's numbers mean
nothing without knowing what it contains.

| script | dataset | what it does |
|---|---|---|
| `fetch_hifi23k.sh` | `D1-HIFI23K` | streams two HG002 PacBio CCS movies from NCBI, keeps reads ≥22 kb |
| `fetch_ont24k.sh` | `D6-ONT24K` | streams 29 HG002 ONT runs from ENA, keeps a 20-28 kb band; ultra-long runs excluded because their yield in that band is only 1.7-2.2% |
| `gen_sim24k.py` | `D2-SIM24K` | simulates 125 000 × 24 kb reads from `hs1.fa`, `ERR = 0.005` substitutions, seed 42, ground truth in the header |

`gen_sim24k.py` is why the accuracy numbers can be read at all: it is **substitutions only, no
indels**, which [`../simulate/measure_error_rate.py`](../simulate/) independently confirms at 0.498%
with a length delta of +0.004%. For generating reads with *controlled* substitution and indel rates,
and with a realistic spread across reads, use [`../simulate/`](../simulate/) instead.

## archive/coverage-ladder/ — the one measurement the suite cannot reproduce

Backs [`../RESULTS.md`](../RESULTS.md) §4. The benchmark suite tops out at 10x (B04) because a 100x
ladder costs hours and told us what it had to tell us — so this run, and only this run, is kept as
the evidence for the claim that nothing degrades at depth: throughput holds within ±1.5% from 10x to
100x while peak memory rises 1.4% for a hundredfold increase in input.

Driver scripts and the summary CSVs are here; the per-run `-x` JSON and `time -v` records were
dropped, since the CSVs carry every figure the section quotes.

---

## What was deleted, and why that is safe

Roughly 400 files of shmap-rs profiling artifacts and the one-off drivers that produced them, all
of which already carried a *"Superseded by RESULTS.md"* banner. Every one measured **shmap-rs**,
which every run re-measures and archives. Keeping stale copies of numbers that are regenerated on
demand is how this repo previously came to hold contradictory figures.
