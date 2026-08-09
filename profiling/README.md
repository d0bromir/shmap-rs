# profiling/

Three live tools, plus the measurements and provenance that **cannot be regenerated**.

Current benchmark numbers are not here — they are in [`../RESULTS.md`](../RESULTS.md), generated
from [`../benchmarks/`](../benchmarks/). This directory used to hold ~400 files of shmap-rs
profiling runs; all of it is now produced on demand by `benchmarks/scripts/run.py`, so it was removed rather
than left to rot into a second, contradictory set of numbers.

What survived is what a re-run cannot give back.

---

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

## other-mappers/ — kept because re-running is prohibitive

| file | what |
|---|---|
| `table1_other_mappers.csv` | minimap2, Winnowmap2, BLEND, mapquik, minSHmap and the C++ `shmap` across four datasets — mapped-at-q60, wrong-at-q60, index/map seconds, memory |
| `wgs_k15_shmap_cpp_py.csv` | shmap-rs / C++ / Python at `k=15` on HiFi, ONT and CLR, whole-genome and chr21 |

**These tools did not change, so their numbers stand.** Re-measuring is not a matter of an idle
afternoon: Winnowmap2 alone took **28 688 s — nearly 8 hours — for one row** of
`table1_other_mappers.csv`, and 8 041 s for another.

Two cautions before quoting them:

- **mapquik reads 0 mapped in every row, and that is an artefact.** It counts newline characters as
  bases, so a line-wrapped reference gives coordinates in file-offset space. Given a one-line
  reference it maps ~99% and agrees with shmap-rs on 96-98% of placements. See
  [`../RESULTS.md`](../RESULTS.md) §8; the current corpus is built by
  `benchmarks/scripts/reference_mappers.py`, which passes it a one-line reference.
- These are older runs at older parameters. For a current, maintained comparison against Winnowmap2
  and mapquik, use the concordance corpus, not this file.

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

Roughly 400 files of shmap-rs profiling artifacts — `sweep_metrics/`, `full_suite_a2/`,
`final_sweep/`, `metrics_bench/`, `real24kbp/`, `ont24kbp/`, `wgs24k/`, `realworld_hifi/`, `old/`,
the loose `*.profile.json`, and the 157 KB `tables.md` dump — plus the drivers that produced them
(`benchmark.py`, `bench_shmaprs_wgs.py`, `extract_tables.py`).

Every one measured **shmap-rs**, which is re-measured on every run by `benchmarks/scripts/run.py`, with the
`-x` reports archived per result set in `benchmarks/results/<suite>/<commit>/raw-profiles.tar.gz`.
`RESULTS.md` §5 is generated from those, so the stage-breakdown tables now describe the commit being
measured rather than whatever was current when someone last edited them by hand.

Each of those directories already carried a *"Superseded by RESULTS.md"* banner. Keeping stale
copies of numbers that are regenerated on demand is how this repo previously came to hold
contradictory figures.
