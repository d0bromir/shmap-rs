# The paper

Two things live here: **the manuscript**, a two-page applications note somebody wrote, and
**its artifacts**, the tables and figures generated from a benchmark result set. Neither
contains a number a person typed.

```
make paper                                  build everything below
make paper-manuscript                       just the manuscript
make paper-check                            fail if any of it is stale (CI runs this)
```

## The notes

Two, both two pages, both hand-written and typeset by the same flow:

| | |
|---|---|
| [`manuscript.tex`](manuscript.tex) | the applications note — what the port is, and what it measures |
| [`optimizations.tex`](optimizations.tex) | the companion — every optimization, by the layer it acts on |

`build_paper.py --list` prints them with their page budgets; `--doc <name>` builds one.

```
python3 benchmarks/scripts/manuscript.py          rebuild generated/macros.{tex,tsv} + MACROS.md
python3 benchmarks/scripts/manuscript.py --check  fail if a macro would change
python3 benchmarks/scripts/manuscript.py --lint   fail if the draft typed a number itself
python3 benchmarks/scripts/manuscript.py --list   every macro's value, source and meaning
python3 benchmarks/scripts/build_paper.py         typeset it to manuscript.pdf
python3 benchmarks/scripts/build_paper.py --check fail if the committed PDF is not current
```

**Its prose contains no numerals.** Every measured quantity is a `\shm…` macro that
`manuscript.py` defines from the promoted result sets, so a sentence reads

```latex
it is \shmSpeedupXMin--\shmSpeedupXMax$\times$ faster single-threaded
```

and a re-measurement rewrites the sentence. That is the whole point: generated tables
removed one staleness failure and left the worse one, because nobody re-reads a paragraph
after a benchmark run and a stale sentence typesets perfectly.

`--lint` is what makes this a checked property rather than a convention. It reads the body
of the draft — not the preamble, not the bibliography, with comments and `\label`/`\url`
arguments and TeX lengths removed — and fails on any digit left over. A line that genuinely
needs one ends with `% lint-ok: <reason>`; the reason is required, because the point is that
somebody looked at the digit, not that the check can be silenced.

Both directions are covered. A macro the draft uses but `manuscript.py` no longer defines
fails `--check` by name, rather than deep inside a TeX log. A macro defined but unused is
reported as a note, because the usual cause is a sentence rewritten to hardcode its number.

**The byline is generated too.** `\shmAuthors`, `\shmOrcids` and `\shmContributors` come
from [`.zenodo.json`](../.zenodo.json), so the paper and the archived DOI cannot name
different people. Zenodo's own split is preserved rather than reinterpreted: `creators`
become the byline, `contributors` are acknowledged. Add an author to the archive record,
not to the draft.

**The optimization table is generated from `PORT_CHANGES.md`**, by
[`optimizations.py`](../benchmarks/scripts/optimizations.py) — the rows of its current-state
table verbatim, plus the `// file:lines` citation of the C++ each one replaces, so
"compared to the C++" is checkable rather than asserted. What that script *declares* rather
than reads is the layer each change acts on (data structure, algorithm, parallelism, code),
which is the paper's classification and not a fact about the repository; it is checked
against the parsed rows both ways, so a new optimization cannot be silently missing from a
note that claims to list them all. See [`generated/OPTIMIZATIONS.md`](generated/OPTIMIZATIONS.md).

**Two pages is enforced, not intended.** `build_paper.py` counts the pages and exits
non-zero over budget; it still writes the PDF, because seeing the overflow is how it gets
fixed. If a re-measurement pushes it over, cut prose — the floats are the evidence.

`manuscript.pdf` is committed and the build is byte-reproducible (see the note on
`SOURCE_DATE_EPOCH` below), so `--check` is a real equality test.

Read [`generated/MACROS.md`](generated/MACROS.md) before quoting any of it: it is generated
from the same declarations that compute the values, so it cannot describe a source the code
does not read, and it carries the caveats — which figures are one favourable row rather than
typical, which are configuration rather than measurement, and which compare two machines
rather than two instruction sets.

# Paper artifacts

Tables and figures — what the manuscript above `\input`s, and what a longer draft
would draw on. Generated from a benchmark result set by
[`benchmarks/scripts/paper.py`](../benchmarks/scripts/paper.py). Nothing in `generated/` is written by hand.

```
python3 benchmarks/scripts/paper.py            # rebuild generated/ from results/suite-1.0/current/
python3 benchmarks/scripts/paper.py --check    # fail if anything is out of date (CI runs this)
python3 benchmarks/scripts/paper.py --list     # what each artifact is built from, without building
python3 benchmarks/scripts/build_pdf.py        # typeset them all into generated/artifacts.pdf
make paper                             # both of the above, from the repo root
```

A benchmark run rebuilds them on its own: `run.py` writes a copy into the result set it just
measured (`<result-set>/paper/`) and typesets it there, so a run is self-describing and ends
with something a person can open. `generated/` here is the copy built from `current/`, and it
updates when a set is promoted.

`artifacts.pdf` **is** committed, and is kept current by the promotion step below.

That is only safe because the build is byte-reproducible: `build_pdf.py` sets
`SOURCE_DATE_EPOCH` from the result set's own measurement date, so TeX stamps the data's date
rather than the moment of the build. Without that every rebuild produced a different file and a
committed PDF would churn on every run. `build_pdf.py --check` is a real equality test for the
same reason.

## Publishing after a benchmark run

```
python3 benchmarks/scripts/promote.py <result-set-dir>            # regenerate + verify
python3 benchmarks/scripts/promote.py <result-set-dir> --commit   # ... and commit
python3 benchmarks/scripts/promote.py <result-set-dir> --push     # ... and push
make promote RESULT_SET=<dir> ARGS=--commit               # same, from the repo root
```

Promotion copies the result set over `current/`, regenerates RESULTS.md, README.md, the paper
artifacts and this PDF, and then re-runs all three `--check`s against what it just wrote. It
refuses a set whose suite version differs from the repo's, or one with recorded failures.

Nothing is committed without `--commit` and nothing is pushed without `--push`: a promotion moves
every headline number in the repository, so it should be a decision rather than a side effect.

**Per-read data must be in the result set, not just in `current/`.** Promotion clears stale
`per-read-*.tsv` before copying, so a set lacking them removes them — and `fig_time_vs_matches`
quietly loses its data. Backfill the *source* set, then promote:

```
python3 benchmarks/scripts/run.py --per-read-stats <result-set-dir>
```

## Using them in the paper

Each artifact is a complete float — `\begin{table}` or `\begin{figure}`, caption and label
included — so a draft `\input`s it and refers to it by the label already inside:

```latex
\usepackage{booktabs}                 % tables
\usepackage{pgfplots}                 % figures
\pgfplotsset{compat=1.18}

\input{paper/generated/x86_64/table_mapper_comparison.tex}
As Table~\ref{tab:comparison} shows, ...
```

`fig_time_vs_matches` additionally needs `\usepgfplotslibrary{fillbetween}` for its
interquartile band; the requirement is stated in a comment at the top of that fragment, because
without the library the band silently disappears rather than erroring.

## The LaTeX engine

`build_pdf.py` uses whichever of tectonic, latexmk, pdflatex, xelatex or lualatex it finds, and
also looks in `~/tools/tectonic/tectonic`. Tectonic is what the benchmark host has: it is a
single binary needing no system TeX installation, and it fetches only the packages a document
actually uses.

A missing engine is **not** an error — the script explains what to install and exits 0, because
a run that produced correct artifacts should not be reported as failed by a previewer.

## What is in here

| file | what it is |
|---|---|
| `table_mapper_comparison.tex` | tools × datasets: mapped, mapq 60, missed %, wrong Q60, index/map seconds, peak memory |
| `table_seed_heuristic.tex` | matches per read, realized and unrealized potential, buckets per read |
| `fig_thread_scaling.tex` | whole-run speedup against thread count, against a linear-speedup line |
| `fig_memory_vs_threads.tex` | peak RSS against thread count, with the C++ as a flat reference |
| `fig_time_vs_matches.tex` | per-read mapping time against matches examined, binned medians with an IQR band |
| `fig_stage_breakdown.tex` | stage shares of `query_mapping`, stacked, per dataset and metric |
| `artifacts.pdf` | all of the above typeset into one document, one artifact per page |
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
