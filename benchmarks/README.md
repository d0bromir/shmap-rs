# benchmarks/

The reproducible benchmark system. See [`../VERSIONING.md`](../VERSIONING.md) for what each
version means and for the rule a pull request has to satisfy.

## Layout

Three folders, by what the contents *are*: declarative inputs, code, and
measured output.

```
benchmarks/
  data/      suite.toml, datasets.tsv, hosts.toml — and files/ (the ~46 GB
             corpus, gitignored, reached through a per-host symlink)
  scripts/   everything that runs
  results/   suite-<v>/<arch>/{ARCH.md, current/, <ver>-<sha>-<date>/}
```

**Results are separated by architecture.** `results/suite-1.0/x86_64/` and
`.../aarch64/` each carry their own `current/` baseline and their own
`ARCH.md` describing the machine. They are kept apart because they are not
comparable: `compare.py` already refuses to diff result sets whose manifests
name different hosts, and this tree is that rule applied to the filesystem,
so a pull request measured on ARM is judged against ARM. The directory name
is `uname -m` verbatim — derived by [`scripts/layout.py`](scripts/layout.py),
never typed — so a run cannot file itself under the wrong architecture.

**The corpus resolves the same way everywhere.** `datasets.tsv` stores paths
relative to `data/files/`, a gitignored symlink to wherever that host keeps
its data. The relative tree below it is identical on every host, so nothing
in the runner is host-aware. See [`data/README.md`](data/README.md).

| path | what | status |
|---|---|---|
| `RUNBOOK.md` | operating the host: launch sequence, traps, recovery | **in place** |
| `data/README.md` | the corpus root, and how to provision a new host | **in place** |
| `data/hosts.toml` | per-host operational facts: address, cores, corpus location | **in place** |
| `scripts/layout.py` | where everything lives; `arch()` and the results paths | **in place** |
| `data/datasets.tsv` | dataset registry — id, host, path, identity triple, provenance | **in place** |
| `scripts/enumerate_datasets.sh` | regenerates the registry's measured columns | **in place** |
| `scripts/validate_suite.py` | checks `suite.toml` resolves against the registry | **in place** |
| `data/suite.toml` | benchmark definitions: which dataset × params × metric × threads | **in place** |
| `run.py` | the runner: lock, authorization, verification, measurement | **in place** |
| `results/<suite>/current/` | the baseline a PR is compared against | **written by run.py** |
| `results/<suite>/<commit>-<date>/` | archived, immutable result sets | **written by run.py** |
| `compare.py` | diffs two result sets and returns the PR verdict as its exit code | **in place** |
| `test_compare.py` | 25 synthetic cases pinning those verdicts; runs in CI | **in place** |
| `report.py` | regenerates the marked regions of `../RESULTS.md` | **in place** |
| `charts.py` | draws pie charts of `profiles.tsv` — the picture of the profiling tables | **in place** |
| `test_charts.py` | aggregation, wedge geometry and partition-guard cases; runs in CI | **in place** |
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
                                     -> profiles.tsv -> charts.py -> chart-*.svg
```

The profiling branch of that chain, spelled out, because every link is a file
somebody can open:

```
shmap -x --profile-log     one JSON report per invocation        profiling
  -> run.py                write_profiles_tsv()                  script
  -> profiles.tsv          one row per invocation, one col/stage  tables
  -> charts.py             reads the table, never the JSON        script
  -> chart-*.svg           + chart-index.html to browse them      graphics
```

`charts.py` deliberately reads `profiles.tsv` rather than `raw/`: the table is
the reviewed, checked-in form of the data, so every wedge traces to a row a
reader can look up, and each chart footers the exact row it came from.

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
results/suite-1.0/<version>-<commit12>-<date>/
  manifest.json          suite + dataset versions, commit, host, who authorized, binaries, duration
  results.tsv            one row per invocation: wall, index, mapping, peak RSS, mapped, mapq60, cmd
  profiles.tsv           one row per invocation, one column per stage — the readable -x view
  checks.tsv             every check, pass/fail, with the detail
  raw-profiles.tar.gz    the full -x JSON reports and time -v records
  chart-*.svg            pie charts drawn from profiles.tsv by charts.py
  chart-index.html       all of the above on one page, for browsing
```

The charts are written by `run.py` at the end of a run and can be regenerated
at any time with `python3 benchmarks/scripts/charts.py` — they are a view of
`profiles.tsv`, never a separate measurement. Time pies are CPU-seconds summed
across threads: `profiles.tsv` warns in its own header that `cpu_*` and
`wall_*` must not be divided into each other, and the charts hold to that.

The directory is named by the **binary's own `--version`**, then the commit, then the date — so a
reader looking for a release's numbers does not have to open a manifest to find the SHA.

`results.tsv` splits the wall into `index_s` and `map_s`. Indexing is a fixed cost set by the
reference and is largely serial; mapping is what scales. `profiles.tsv` goes further, one column per
stage — note its `wall_*` columns are wall-clock while `cpu_*` are summed across threads, so the
latter exceed the former at high thread counts and must never be divided into each other.

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

**mapquik needs a one-line reference.** It counts newline characters as bases, so a line-wrapped
FASTA gives coordinates in file-offset space — silently. Against `hs1.fa` (wrapped at 50 columns) it
reported every chromosome at exactly 1.02x its true length and placed 3 of 39 965 simulated reads
correctly; with a one-line reference it places 98.09%. `suite.toml` gives it a `reference_override`
and the `awk` recipe to build one, and a missing override file fails the run rather than silently
using the wrapped reference.

Smoke-tested end to end on B05 (30 invocations, 12.5 min): all nine checks passed and the numbers
reproduced the hand-run measurements — Containment `-@1` 24.53 s against 24.13 s measured manually,
C++ 53.5 s against 54.29 s, agreement 0.9792 identical.
