# benchmarks/

The reproducible benchmark system. See [`../VERSIONING.md`](../VERSIONING.md) for what each
version means and for the rule a pull request has to satisfy.

## Layout

| path | what | status |
|---|---|---|
| `datasets.tsv` | dataset registry — id, host, path, identity triple, provenance | **in place** |
| `enumerate_datasets.sh` | regenerates the registry's measured columns | **in place** |
| `validate_suite.py` | checks `suite.toml` resolves against the registry | **in place** |
| `suite.toml` | benchmark definitions: which dataset × params × metric × threads | **in place** |
| `run.py` | the runner: lock, authorization, verification, measurement | **in place** |
| `results/<suite>/current/` | the baseline a PR is compared against | **written by run.py** |
| `results/<suite>/<commit>-<date>/` | archived, immutable result sets | **written by run.py** |
| `compare.py` | diffs two result sets and returns the PR verdict as its exit code | **in place** |
| `test_compare.py` | 25 synthetic cases pinning those verdicts; runs in CI | **in place** |
| `report.py` | regenerates the marked regions of `../RESULTS.md` | **in place** |
| `reference_mappers.py` | builds the cached external-mapper corpus (once, not per PR) | **in place** |
| `concordance.py` | scores a shmap-rs PAF against a cached external PAF | **in place** |
| `test_concordance.py` | interval-arithmetic and scoring cases; runs in CI | **in place** |
| `results/reference-mappers/manifest.json` | corpus provenance: versions, commands, counts | **in place** |

## datasets.tsv

Every input this project measures against, keyed by a stable id. The **identity triple** — bytes,
records, bases — is re-checked before a run measures anything; a mismatch fails the run instead of
quietly benchmarking a different file. That guard exists because the most damaging failure here is
not a crash, it is attributing a dataset change to a code change.

Ids beginning `D<n>-` are the datasets `../RESULTS.md` reports on. `DX-` are available but not
part of the current suite (subsets, fixtures, or platform sets kept for context).

Regenerating a file means **adding a new id**, never editing an existing row — historical result
sets have to keep resolving to what they actually measured.

## Current state of migration

The benchmark scripts under `../profiling/*/` are the historical drivers: each was written for one
investigation, and each carries its own copy of the parameters. They stay as the provenance record
of published numbers. The point of this directory is to replace them with a single definition and
a single runner, so that a PR can be measured by one command rather than by remembering which of
nineteen scripts applies.

**The migration is complete.** One command measures a PR, judges it, and can post the verdict:

```sh
./run.py --pr 123 --post
```

Its exit code is the verdict — 0 ACCEPT, 1 REVIEW, 2 BLOCK, 3 not comparable — so it can gate a
merge directly. The full chain a reader can walk in either direction:

```
datasets.tsv  ->  suite.toml  ->  results/<suite>/<commit>/results.tsv
                                     -> raw/ (-x reports behind each row)
                                     -> compare.py (verdict vs current/)
                                     -> report.py  -> ../RESULTS.md
```

What is still hand-run rather than wired in: `../profiling/adjudicate_disagreements.py`, which
scores disagreements against ground truth and is an investigation tool, not a gate.

## suite.toml

Five benchmarks (B01–B05), three metrics each, seven thread counts, against both implementations.
`validate_suite.py` checks it before a run commits an hour to it.

| | invocations | wall |
|---|---:|---:|
| PR run — shmap-rs only, C++ from cache | 105 | ~78 min |
| C++ re-measure — 3x for a median, only when it is rebuilt | 45 | ~187 min |

One tier, no fast/slow split: a gate that gets skipped catches nothing, and a2 is idle and free.
B04 (10x depth) is ~52 min of the PR run on its own and stays anyway — it is the only benchmark
where indexing is a small enough share of the wall for mapping scaling to be read directly.

The C++ is measured three times and reduced by median because it varies ~8% run-to-run on this
host, which is enough to move a quoted speedup by a tenth. `run.py` will refuse a C++ binary
containing live Tracy symbols: upstream's Makefile adds `-DTRACY_ENABLE` unconditionally and it
costs ~8.8%, which would silently flatter us.

## run.py

Executes on `a2` only. Three things it does before it will measure anything:

1. **Takes a host-wide exclusive `flock`.** At most one benchmark runs at a time no matter how many
   PRs are open; concurrent callers queue (`--no-wait` to fail instead). It is a kernel lock, so it
   is released even if the process is killed — there is no stale state to clear. Two runs sharing
   64 cores would contaminate each other's timings and produce results that are wrong rather than
   obviously broken.
2. **Authorizes the commit.** `--pr N` builds nothing until the author has write access, or a
   write-access user applied `bench-approved` *and* the PR has not been pushed to since. Who
   authorized it goes in the manifest.
3. **Verifies every dataset** against the registry's identity triple, and refuses to run if an
   input has changed.

```sh
./run.py --status                 # is anything running?
./run.py --dry-run                # print the 105 planned invocations
./run.py --commit <sha>           # measure a trusted commit
./run.py --pr 42                  # measure a PR, subject to authorization
./run.py --impls shmap-rs,cpp-shmap   # also re-measure the reference (~187 min)
```

### What a run produces

```
results/suite-1.0/<commit>-<date>/
  manifest.json    suite + dataset versions, commit, host, who authorized, binaries, duration
  results.tsv      one row per invocation: wall, peak RSS, mapped, mapq60, and the exact command
  checks.tsv       every check, pass/fail, with the detail
  raw/             -x JSON reports and time -v records
```

`results.tsv` carries the full command line for every row, so any number resolves to an exact
invocation against an exact input. Reference-impl repeats are reduced by median and marked
`median3` in the `repeat` column.

Large PAFs are deleted once the checks that need them have run — B04 writes ~600 MB per
invocation, and keeping 21 of those per metric would be ~12 GB for one benchmark.

### External mappers

Winnowmap2 and mapquik are run **once per (mapper, benchmark)** by `reference_mappers.py` and
cached on the host; `run.py` never invokes them, it joins against the cached PAFs. The whole corpus
is ~3.6 h of one-time cost that no PR run pays again — Winnowmap2 alone is 150 min on B04.

```sh
./reference_mappers.py --list     # what is cached, what is stale
./reference_mappers.py --run      # build whatever is missing
```

A cache entry is keyed on mapper version, full command line and both inputs' identity, so a changed
input makes it stale rather than silently reused.

**These are concordance numbers, not accuracy.** Winnowmap2 is the most accurate long-read mapper
available and is still an estimate: where it and shmap-rs disagree, nothing here says which is
right. Accuracy comes from B02, whose reads carry true positions. See `../RESULTS.md` section 8.

**mapquik's coordinates are not currently comparable.** It reports reference lengths exactly 1.02x
the true ones and places only 3 of 39 965 simulated reads within a read length of their true
position — its PAF positions are not in reference coordinate space in this configuration, most
likely because homopolymer compression is on by default (`--nohpc`). Its concordance figure is
therefore meaningless and must not be read as disagreement with shmap-rs. Its mapped counts and
timings are still valid.

Smoke-tested end to end on B05 (30 invocations, 12.5 min): all nine checks passed and the numbers
reproduced the hand-run measurements — Containment `-@1` 24.53 s against 24.13 s measured manually,
C++ 53.5 s against 54.29 s, agreement 0.9792 identical.
