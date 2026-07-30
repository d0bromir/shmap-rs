# benchmarks/

The reproducible benchmark system. See [`../VERSIONING.md`](../VERSIONING.md) for what each
version means and for the rule a pull request has to satisfy.

## Layout

| path | what | status |
|---|---|---|
| `datasets.tsv` | dataset registry — id, host, path, identity triple, provenance | **in place** |
| `suite.toml` | benchmark definitions: which dataset × params × metric × threads | planned (step 2) |
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
