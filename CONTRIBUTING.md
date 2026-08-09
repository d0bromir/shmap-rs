# Contributing

Pull requests are welcome. This document is the whole process: what runs automatically, what a
maintainer runs by hand, and what decides whether a change merges.

The short version: **speed never outranks accuracy.** A change that is faster everywhere and maps
one read fewer is blocked, not traded off.

---

## 0 How work is organised

Most work here answers a question or change request from Pesho, the C++ `shmap` author.
[`QUESTIONS.md`](QUESTIONS.md) is the log of those: what was asked, what was done, what the
benchmark said.

**One question at a time, one branch and one PR each.**

1. Cut a branch from an up-to-date `main`, named for the question (`q3-frequent-kmer-filter`).
2. Work in steps, running the relevant tests as you go rather than only at the end — §1's cheap
   tier is ~2.5 minutes and catches most of it.
3. When the change is complete, open a PR and add its row to `QUESTIONS.md`.
4. A maintainer runs the benchmark against the PR (§1), and its verdict decides the merge (§2).

Do not batch several questions into one branch. The benchmark verdict is only useful if it is
attributable to a single change: batched, a regression cannot be traced and a good change cannot be
defended. The same applies to unrelated cleanups — they get their own PR.

---

## 1 Two tiers of checking

| tier | where | when | cost |
|---|---|---|---|
| **Cheap** | GitHub-hosted runner | automatically, every push and PR | ~2.5 min |
| **Benchmarks** | the private host `a2` | on request, by a maintainer | ~78 min |

### The cheap tier — automatic

`.github/workflows/ci.yml` runs on every push and pull request:

`cargo fmt --check` · `cargo build --release --locked` · `cargo clippy -D warnings` ·
`cargo test --release` · `cargo test` (debug) · `benchmarks/scripts/test_layout.py` ·
`benchmarks/scripts/validate_suite.py` ·
`benchmarks/scripts/test_compare.py` · `benchmarks/scripts/test_concordance.py` · `benchmarks/scripts/test_run.py` ·
`benchmarks/scripts/test_charts.py` · `benchmarks/scripts/report.py --check` · `benchmarks/scripts/charts.py --check` ·
`benchmarks/scripts/paper.py --check`

Two of those are easy to trip over:

- **Debug tests are not redundant.** They activate the `debug_assert!`s that pin the risky designs —
  that `best_fixed_length` restores `diff_hist` exactly, that the parallel reader's second pass
  writes what its first pass counted, that a segment's parts tile its buffer with no gaps.
- **`report.py --check` fails if `RESULTS.md` *or* `README.md` was hand-edited.** Both carry
  generated regions; regenerate with `python3 benchmarks/scripts/report.py`. See §5.

**Do not add a `uses:` line to the workflow.** The repository is set to `allowed_actions: local_only`,
so a third-party action does not produce a failing step — it aborts the whole run as
`startup_failure` with an empty log, which reads like a GitHub outage. Four commits merged unchecked
before anyone noticed. The workflow is written to need no actions at all.

### The benchmark tier — on request

Benchmarks build and execute the PR's code, so they run on a private host behind an authorization
gate rather than on a runner. See [`SECURITY.md`](SECURITY.md) for why `a2` is deliberately not a
GitHub self-hosted runner.

A maintainer runs, on `a2`:

```bash
python3 benchmarks/scripts/run.py --pr 123 --post
```

That single command authorizes, measures, compares against the baseline, and posts the verdict to
the PR. Its exit code *is* the verdict: `0` ACCEPT, `1` REVIEW, `2` BLOCK, `3` not comparable.

It refuses to build or execute anything until it has confirmed against the GitHub API that **either**
the PR author has push/admin, **or** a user with push/admin applied the `bench-approved` label — and
that no push has landed since the label. The label is the human review step: someone reads the diff
before the code runs.

Operating the host — launch sequence, the traps, and how to re-judge a run without
re-measuring it — is in [`benchmarks/RUNBOOK.md`](benchmarks/RUNBOOK.md).

At most one benchmark runs at a time, host-wide. Concurrent invocations queue on a kernel file lock
rather than failing, so two of them cannot contaminate each other's timings.

---

## 2 What decides the merge

`benchmarks/scripts/compare.py` applies the table in [`VERSIONING.md`](VERSIONING.md) mechanically, with the
numbers from `benchmarks/data/suite.toml`:

| verdict | condition |
|---|---|
| **BLOCK** | fewer reads mapped, fewer at mapq 60, ground truth down, or C++ agreement down |
| **BLOCK** | a blocking check failed — `thread_determinism`, `validate_paf`, `ground_truth` |
| **BLOCK** | wall time >10% worse on a benchmark, a non-zero exit, or an incomplete run |
| **REVIEW** | wall time >3% worse, or peak RSS >5% worse — justify or fix |
| **ACCEPT** | within noise or better, no accuracy change |
| **ERROR** | the two sets are not comparable at all — different suite MAJOR, dataset version, or host |

Wall-time verdicts use the geometric mean across thread counts within a benchmark, not individual
rows: shmap-rs is measured once per configuration, so a single row carries this host's 1-2% noise,
and testing ~105 of them against a 3% line would flag several every run by chance.

**If your change alters output on purpose** — a correctness fix, where the previous output was wrong
— say so in the PR, and a maintainer re-runs with `--allow-output-change`, which downgrades the
accuracy blocks to REVIEW. Name the affected records in the commit message, as `8bc38f1` does.

---

## 3 Before you open a PR

```bash
cargo fmt && cargo clippy --release --all-targets -- -D warnings
cargo test --release && cargo test
python3 benchmarks/scripts/validate_suite.py
```

That is the cheap tier, locally. It takes about a minute and saves a round trip.

Match the surrounding code: this codebase carries comments explaining *why* a design is the way it
is, especially where the obvious alternative was measured and lost. Several optimizations here were
reverted for being slower; the comments recording that are load-bearing and should not be dropped.

**This workload is memory-latency bound.** Adding a level of indirection to shrink a struct has been
measured as a net loss more than once. Measure before and after; do not reason about it.

---

## 4 Adding or changing a benchmark

Everything measured is defined in `benchmarks/data/suite.toml`. If a parameter is not in that file, it is
not passed to the binary — do not add flags in a script.

- **Adding** a benchmark is a `suite_version` MINOR bump. Old results stay comparable.
- **Changing** an existing one — parameters, thread counts, dataset binding — is a MAJOR bump, and
  results across that boundary must not be diffed. `compare.py` enforces this and returns ERROR.
- **Datasets are append-only.** A regenerated input gets a new id in `benchmarks/data/datasets.tsv`,
  never a redefinition, so historical results keep resolving to what they actually measured. Every
  run re-checks each file's identity and fails rather than measuring a changed input.

---

## 5 Results and reporting

`RESULTS.md` is the single place benchmark numbers live, and `README.md`'s headline figures are
generated from the same result set. The regions between `<!-- BEGIN GENERATED -->` markers are
produced by `benchmarks/scripts/report.py` — edit them and CI fails. The prose around them is written by
people and is not derivable from a TSV.

README.md was added as a target after its headline table drifted for several commits: it advertised
46.2 s against the C++'s 98.3 s long after the measured pair was 47.6 and 104.1. Nothing caught it
because `--check` only looked at RESULTS.md.

```bash
python3 benchmarks/scripts/report.py            # regenerate from results/suite-<v>/current/
python3 benchmarks/scripts/report.py --check    # what CI runs
```

The paper's tables and figures are generated the same way and from the same result set, into
`paper/generated/`. A benchmark run rebuilds them for the set it just measured; `make paper`
rebuilds the repo copy after a set is promoted.

```bash
python3 benchmarks/scripts/paper.py             # regenerate paper/generated/
python3 benchmarks/scripts/paper.py --check     # what CI runs
python3 benchmarks/scripts/paper.py --list      # each artifact's inputs and transformation
python3 benchmarks/scripts/build_pdf.py         # typeset them into generated/artifacts.pdf
python3 benchmarks/scripts/build_pdf.py --check # fail if the committed PDF is stale
make paper                              # regenerate and typeset
```

`paper/generated/artifacts.pdf` is committed. That works only because the build is
byte-reproducible: `build_pdf.py` sets `SOURCE_DATE_EPOCH` from the result set's measurement date,
so the same artifacts always typeset to the same bytes. CI does not check it — the runner has no
LaTeX engine, and `build_pdf.py` exits 0 rather than claiming a verdict it cannot reach. It is
checked on the benchmark host during promotion instead.

**Publishing a finished run:**

```bash
python3 benchmarks/scripts/promote.py <result-set-dir> --commit
```

That copies the set over `current/`, regenerates RESULTS.md, README.md, the paper artifacts and
the PDF, re-runs all three `--check`s against what it wrote, and commits. `--push` pushes. It
refuses a set whose suite version differs from the repo's or that recorded failures.

`fig_time_vs_matches` needs per-read data, which a run collects only when `[per_read_stats]` is
enabled in `suite.toml`. A result set measured before that existed can be given the data
afterwards without re-measuring anything else:

```bash
cargo build --release
python3 benchmarks/scripts/run.py --per-read-stats benchmarks/results/suite-1.0/current
```

That records the commit that produced the rows separately in the manifest, because they come
from a later binary than the timing rows beside them.

`paper/generated/PROVENANCE.md` is generated from the same `Artifact` declarations that build the
files, so it cannot document a transformation the code does not perform. Adding an artifact means
declaring its sources, transformation, presentation and caveats — there is no separate place to
write them down, and no way to skip it.

External mappers (Winnowmap2, mapquik) are a **concordance** corpus, not ground truth. They are run
once per dataset by `benchmarks/scripts/reference_mappers.py` and cached; `run.py` only joins against them.
Where shmap-rs and Winnowmap2 disagree, nothing says which is right — accuracy claims come from B02,
whose reads carry true positions. Report the two separately and label them.

---

## 6 Releases

Software version is SemVer, no `v` prefix (tags are `1.2.0`).

| bump | when |
|---|---|
| MAJOR | output changes in a way that is not a bug fix |
| MINOR | new capability or a performance change with identical output |
| PATCH | bug fixes that correct wrong output, and doc/build changes |

Output-affecting *fixes* are PATCH even though the bytes change — the previous output was wrong.

```bash
# 1. bump Cargo.toml, then rebuild WITHOUT --locked so Cargo.lock follows.
#    `--locked` refuses the bump until the lock is regenerated, and the binary
#    keeps reporting the old version until you do.
cargo build --release && ./target/release/shmap --version   # confirm before tagging

# 2. commit the bump, then tag THAT commit by SHA
git commit -am "Release 1.2.1" && git push origin main
SHA=$(git rev-parse HEAD)
git tag -a 1.2.1 "$SHA" -F release-notes.md
git push origin 1.2.1

# 3. publish
gh release create 1.2.1 --verify-tag --notes-file release-notes.md
```

**Pass the SHA explicitly.** `git tag -f -a` without one retargets `HEAD`, which has silently moved
a published tag in this repo before.

**Verify `--version` before tagging.** Cutting 1.3.0, the first build after the bump still reported
`1.2.0`: `Cargo.lock` pins the crate's own version, and `--locked` refuses to update it. That was
caught before tagging only because `--locked` failed loudly — build without it, then read
`--version` back.

### Correcting notes after publishing

Release notes live in **two** places that can diverge: the git tag annotation and the GitHub release
body. `gh release edit --notes-file` updates only the second. That has already happened here —
`git show 1.3.0` served pre-correction figures while the web page served corrected ones.

Update both, and take the notes from the release body so there is one source:

```bash
gh release edit 1.3.0 --notes-file corrected.md

SHA=$(git rev-list -n1 1.3.0)                    # capture BEFORE re-tagging
gh release view 1.3.0 --json body -q .body > /tmp/notes.md
git tag -f -a 1.3.0 "$SHA" -F /tmp/notes.md
[ "$(git rev-list -n1 1.3.0)" = "$SHA" ] || { echo "tag moved — do not push"; exit 1; }
git push --force origin 1.3.0
```

The check is not ceremony: force-pushing a tag that has quietly retargeted rewrites what a published
version means, and nothing downstream would tell you.

Nothing automated guards this. `report.py --check` covers `RESULTS.md` and `README.md`, but a tag
annotation is not in the working tree and no CI job can see it.
