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
| `run.sh` | one entry point that runs the suite and writes a result set | planned (step 3) |
| `results/<suite>/current/` | the baseline a PR is compared against | planned (step 3) |
| `results/<suite>/<commit>-<date>/` | archived, immutable result sets | planned (step 3) |
| `compare.py` | diffs two result sets and applies the PR rule | planned (step 4) |
| `report.py` | regenerates `../RESULTS.md` from a result set | planned (step 5) |

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

Migration order and rationale are in `../VERSIONING.md`; the steps are listed in the table above.

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
