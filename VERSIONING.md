# Versioning

Four things in this project are versioned independently, and a benchmark result is only meaningful
when all four are stated. This document defines them and the rule a pull request has to satisfy.

| # | what | where it lives | changes when |
|---|---|---|---|
| 1 | **Software version** | `Cargo.toml`, git tag, GitHub release | code changes |
| 2 | **Benchmark suite version** | `benchmarks/data/suite.toml` → `suite_version` | the *definition* of a benchmark changes |
| 3 | **Dataset version** | `benchmarks/data/datasets.tsv` → `dataset_version` | any input file is added, replaced or regenerated |
| 4 | **Result set** | `benchmarks/results/<suite>/<commit>/` | every run |

---

## 1 Software version — SemVer

`MAJOR.MINOR.PATCH`, no `v` prefix (tags are `1.2.0`, matching `1.1.0` and `1.0.0`).

| bump | when |
|---|---|
| **MAJOR** | PAF output changes in a way that is not a bug fix — different coordinates, different mapping decisions, removed CLI flags |
| **MINOR** | new capability or a performance change with identical output: new flags, new metric, threading, optimizations |
| **PATCH** | bug fixes that correct wrong output, and doc/build changes |

**Output-affecting fixes are PATCH, not MAJOR**, even though the bytes change — the previous output
was wrong. Say so explicitly in the release notes and name the affected records, as `8bc38f1` does.

The release process is in [`CONTRIBUTING.md`](CONTRIBUTING.md). In short: bump `Cargo.toml`, commit,
annotate a tag with the release notes, push tag, `gh release create --verify-tag`.

## 2 Benchmark suite version

`benchmarks/data/suite.toml` defines *what* is measured: which datasets, which parameter sets, which
metrics, which thread counts. Its `suite_version` is `MAJOR.MINOR`:

- **MINOR** — a benchmark is *added*. Old results stay comparable; the new row simply has no history.
- **MAJOR** — an existing benchmark's definition changes (parameters, thread counts, dataset
  binding). **Results across a MAJOR boundary are not comparable** and must not be diffed.

This is the version that makes PR comparison sound. Two result sets may only be compared when their
`suite_version` MAJOR agrees.

## 3 Dataset version

`benchmarks/data/datasets.tsv` is the registry. Every input file carries an identity triple —
**bytes, records, bases** — which is what a run records and re-checks.

A run **fails** rather than silently proceeding if a dataset's triple does not match the registry.
That is the guard against the most damaging failure mode here: benchmarking against a file that was
quietly regenerated or truncated, and attributing the difference to code.

Datasets are append-only. A regenerated file gets a **new id**, never a redefinition of an old one,
so historical results keep pointing at what they actually measured.

## 4 Result sets

```
benchmarks/results/
  suite-1.0/                              # suite_version MAJOR
    current/                              # the baseline a PR is compared against
      manifest.json                       # versions, commit, host, date, binaries
      results.tsv                         # one row per (benchmark, metric, threads, impl)
      profiles.tsv                        # one row per invocation, one column per stage
      checks.tsv                          # every check, pass/fail, with the detail
      raw-profiles.tar.gz                 # the full -x JSON reports and time -v records
    1.3.0-4c36739d9c85-2026-08-01/        # archived, immutable, one per accepted run
    1.2.0-b3d67e2ba86e-2026-07-31/
```

**Archived sets are named `<version>-<commit12>-<date>`.** The version is the binary's own
`--version`, so it records what actually ran rather than what a tag later claimed — a run measured
before a release bump carries the version it was built with, and the commit disambiguates. Looking
for "the 1.3.0 numbers" should not require opening a manifest.

`profiles.tsv` is the readable form of the `-x` reports: one row per invocation, one column per
stage, greppable and diffable in review. The tarball keeps full fidelity, but data committed to be
read has to be readable without unpacking it first.

`current/` is a copy, not a symlink, so a checkout always has a usable baseline. Every archived set
is immutable once written. **Historical sets are never edited** — if a number was wrong, add a new
set and note it; do not rewrite history that a paper may already cite.

---

## The PR rule

A pull request that touches `src/` must carry a result set produced from its own head commit, at the
**same `suite_version` MAJOR** as `current/`, on the **same host**.

The comparison against `current/` decides the merge:

| outcome | condition | action |
|---|---|---|
| **Blocked** | any accuracy regression — fewer reads mapped, fewer at mapq 60, or a ground-truth drop | do not merge, regardless of speed |
| **Blocked** | any logical-invariant violation (`profiling/validate_paf.py`) | do not merge |
| **Blocked** | output differs across thread counts | do not merge |
| **Review** | wall time regresses past that host's review threshold on any benchmark | justify or fix |
| | *(measured per benchmark, aggregated across thread counts — see below)* | |
| **Accept** | output byte-identical, wall time within noise or better | merge; refresh `current/` if it improves |
| **Accept, output changed** | output differs *and* the change is an argued correctness fix | merge with the reasoning in the commit, and archive the old set |

Speed never outranks accuracy. "Never degrade mapping" is the gate; throughput is the goal behind it.

### Why wall time is judged per benchmark, not per measurement

`compare.py` takes the geometric mean of the per-thread-count ratios within a benchmark rather than
testing each of the ~105 measurements against the threshold. That is not a softening of the rule —
it is what makes the rule mean anything.

Two full runs of **behaviourally identical code** (`93ced64` and `6d6f6a4`, whose only differences
default to off, with identical mapped counts) were compared:

| | range across benchmarks |
|---|---|
| aggregated per benchmark | **-2.2% to +2.8%** — all inside the review band |
| worst single thread count | **+11.6%** (B04/Jaccard at `-@2`), also +9.7%, +9.3%, +9.1% |

Judged per measurement, that pair would have **BLOCKED** on the block rule and raised several
REVIEWs, on a change that does nothing. shmap-rs is measured once per configuration, so a single
row carries the host's full run-to-run noise; testing a hundred of them against one line guarantees
false positives. The aggregate is the honest signal, and the worst row is reported alongside it for
context without deciding anything.

This is also the argument against "just re-run it until it's green": a gate that fails at random
teaches people to ignore it.

### The thresholds themselves are per host

`suite.toml` defines the matrix — the same on every machine, or results do not mean anything — but
run-to-run noise is a property of the *machine*, so a host may override the wall-time thresholds in
`benchmarks/data/hosts.toml`. `a2` reviews at **6%** and blocks at **12%** instead of suite.toml's
3% and 10%, and takes `repeats = 3`; `galaxy` keeps the defaults with `repeats = 1`, because two
runs of one commit there agreed within ~1% on every row.

**Accuracy thresholds are not overridable**, and `compare.py` refuses any host override outside the
wall-time set: a drop in mapped reads is a regression on any machine. Each host's threshold, the
measurement behind it, and how to re-derive it are in that host's `ARCH.md` under
`benchmarks/results/suite-1.0/<arch>/`.

### Comparing against other mappers

shmap-rs results are **regenerated on every run**. Other mappers (minimap2, winnowmap2, blend,
mapquik, and the C++ `shmap`) are measured **once per suite version** and cached, because they do
not change between our PRs. Their cached numbers carry the same manifest fields, so it is always
visible which binary and host they came from.

The C++ `shmap` is the exception that matters most: it is the reference implementation, so it is
re-measured whenever *it* is rebuilt, and its build flags are recorded — it must be built **without**
`-DTRACY_ENABLE`, which the upstream Makefile adds by default and which costs it ~8.8%.

---

## Traceability

The chain a reader has to be able to walk, in both directions:

```
datasets.tsv (id, path, bytes/records/bases)
   -> suite.toml (which params, metrics, thread counts apply to that id)
      -> results/<suite>/<commit>/results.tsv (one row per measurement)
         -> results/<suite>/<commit>/raw/ (the -x report behind that row)
            -> RESULTS.md and README.md (tables and conclusions, generated)
```

Every results row names its dataset id, parameter set id, metric, thread count, implementation and
binary version, so any number in `RESULTS.md` resolves to an exact command line and an exact input
file. `RESULTS.md` is **generated**, never hand-edited — editing it by hand breaks the chain and is
how the contradictory figures this project already had came about.
