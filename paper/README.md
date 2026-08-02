# Paper artifacts

Tables and figures for the paper, generated from a benchmark result set by
[`benchmarks/paper.py`](../benchmarks/paper.py). Nothing in `generated/` is written by hand.

```
python3 benchmarks/paper.py            # rebuild generated/ from results/suite-1.0/current/
python3 benchmarks/paper.py --check    # fail if anything is out of date (CI runs this)
python3 benchmarks/paper.py --list     # what each artifact is built from, without building
make paper                             # same as the first, from the repo root
```

A benchmark run rebuilds them on its own: `run.py` writes a copy into the result set it just
measured (`<result-set>/paper/`), so a run is self-describing. `generated/` here is the copy
built from `current/`, and it updates when a set is promoted.

## Using them in the paper

Each artifact is a complete float — `\begin{table}` or `\begin{figure}`, caption and label
included — so a draft `\input`s it and refers to it by the label already inside:

```latex
\usepackage{booktabs}                 % tables
\usepackage{pgfplots}                 % figures
\pgfplotsset{compat=1.18}

\input{paper/generated/table_mapper_comparison.tex}
As Table~\ref{tab:comparison} shows, ...
```

Neither package is installed on the benchmark host, so the fragments are emitted there but
never compiled. Compile them wherever the paper is built.

## What is in here

| file | what it is |
|---|---|
| `table_mapper_comparison.tex` | tools × datasets: mapped, mapq 60, missed %, wrong Q60, index/map seconds, peak memory |
| `table_seed_heuristic.tex` | matches per read, realized and unrealized potential, buckets per read |
| `fig_thread_scaling.tex` | whole-run speedup against thread count, against a linear-speedup line |
| `fig_memory_vs_threads.tex` | peak RSS against thread count, with the C++ as a flat reference |
| `fig_stage_breakdown.tex` | stage shares of `query_mapping`, stacked, per dataset and metric |
| `*.tsv` | exactly the numbers the matching `.tex` draws |
| `PROVENANCE.md` | per artifact: inputs down to the column, transformation, presentation, caveats |

**Read `PROVENANCE.md` before quoting any of it.** It is generated from the same declarations
that build the artifacts, so it cannot describe a transformation the code does not perform, and
it carries the caveats that decide whether a number means what it looks like — which rows are
threaded and which are single-threaded, which columns are unknowable rather than zero, and which
ratio is an upper bound rather than a measurement.

## Why the `.tsv` beside every `.tex`

The `.tex` is for the typesetter and the `.tsv` is for everyone else. A reviewer can check a bar
in a figure without a LaTeX toolchain, `git diff` shows what a re-measurement actually moved
instead of a wall of changed coordinates, and the numbers stay greppable when the paper is not
being built. Both come out of one builder, so they cannot disagree.

## Reproducibility

Output is a pure function of the result set: no generation timestamps, no locale-dependent
formatting, sorted iteration throughout. Regenerating over one result set produces byte-identical
files, which is what makes `--check` a real equality test rather than a smoke test.

Every file's header names the result set, its commit, the host, the measurement date, and a
sha256 over the input files. A fragment sitting in a draft months from now still says which
measurement produced it, and the digest catches a result set that was edited after the fact.
