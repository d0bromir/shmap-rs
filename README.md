# shmap-rs

A Rust port of [`shmap`](https://github.com/pesho-ivanov/shmap) — a sketch-based long-read mapper
that finds where a read belongs by k-mer set overlap rather than by alignment.

**Latest release: [1.4.0](https://github.com/d0bromir/shmap-rs/releases/tag/1.4.0)** — per-read
instrumentation (`--per-read-stats`), and the first release measured on two architectures with a
working host-drift correction. Mapping output is unchanged.

<!-- BEGIN GENERATED: readme-pitch -->
Against the C++ original on real whole-genome data: **2.1–3.0x faster single-threaded, up to 16.6x
with threads**, with identical mapping counts. Memory is ~7.1x lower single-threaded (2.67 GB against 18.85 GB) — but it grows with thread count, reaching 8.78 GB at the highest, so size for the run you intend.
<!-- END GENERATED: readme-pitch -->

---

## Results at a glance

Real HG002 reads against the whole T2T-CHM13 genome (3.117 Gbp), on a 64-core host.
The C++ is single-threaded by design, so `-@1` is the like-for-like column.

<!-- BEGIN GENERATED: readme-summary -->
| dataset | shmap-rs `-@1` | shmap-rs `-@4` | C++ `shmap` | speedup | memory |
|---|---:|---:|---:|---:|---:|
| HiFi, 23.2 kb, 149 438 reads | **41.1 s** | **14.8 s** | 109.3 s | **2.66x** / 7.37x | 2.64 GB vs 18.85 GB |
| ONT, 23.8 kb, 92 220 reads | **20.1 s** | **8.5 s** | 57.2 s | **2.84x** / 6.70x | 2.66 GB vs 18.85 GB |

Across all 5 benchmarks and three metrics: **2.14–2.97x** single-threaded, **6.44–7.59x** at `-@4`. Every figure here is generated from the current result set and checked in CI — see [RESULTS.md](RESULTS.md).

- **Scales to many threads** — up to **16.64x** whole-run at `-@ 32`; the C++ cannot use more than one core. Output is byte-identical at every thread count.
<!-- END GENERATED: readme-summary -->

- **Memory is flat in coverage** — 2.13 GB at 1x, 2.16 GB at **100x** (311.7 Gbp of reads in one
  run), from the archived ladder at `8bc38f1`; the level has moved since, the flatness has not.
  The C++ sits at 18.85 GB regardless.
- **Identical mapping** — same reads mapped, same mapq-60 counts as the C++; 98.3% of records agree
  byte-for-byte, the rest being adjacent-bucket ties broken differently by a stable sort.

### Accuracy in repeats is a sampling choice, not a limit of the method

Measured against 125 000 simulated reads whose true positions are known, and against
[Winnowmap2](https://github.com/marbl/Winnowmap), the most accurate long-read mapper available:

| | correct placements | wall |
|---|---:|---:|
| shmap-rs, `-r 0.01` (paper default) | 99.187% | **7.6 s** |
| C++ `shmap`, `-r 0.01` | 99.19% | 88.4 s |
| **shmap-rs, `-r 0.10`** | **99.626%** | **68.6 s** |
| Winnowmap2 | 99.65% | 1 008 s |

At the default sampling rate, **81% of shmap-rs's placement errors are in satellite/rDNA regions**
— a 10.4% error rate there against 0.16% elsewhere. Three attempts to fix that by changing the
*scoring* all failed, and measurably made it worse. The cause is that FracMinHash at `-r 0.01`
keeps 1% of k-mers uniformly at random, so most reads retain none of the rare variants that
distinguish one repeat copy from another: there is no signal for a scoring rule to use.

Sampling more k-mers fixes what no scoring change could. At `-r 0.10` shmap-rs lands **within 30
reads of Winnowmap2, roughly 15x faster** — and still faster than the C++ at its own default, which
is 0.44 points less accurate.

**That setting is not free, and is not the default.** It costs 6.6x wall and **7x peak memory —
17.0 GB against 2.4 GB**, which is the same order as the C++'s 18.85 GB and gives up the memory
advantage above. On real reads the gain is smaller than on simulated (+0.57 pp of concordance). The
point is what it says about the method — the ceiling usually attributed to sketch-based mapping in
repeats belongs to the *sampling rate*, not to the approach — not that anyone should run `-r 0.10`
by default.

Full tables, all three scoring metrics, six datasets, the correctness checks, and the rejected
approaches with their data: **[RESULTS.md](RESULTS.md)**.

## Install

```sh
cargo build --release      # target/release/shmap
```

Requires a recent stable Rust (edition 2024). No system dependencies.

## Usage

```sh
shmap -s reference.fa -p reads.fa -k 25 -r 0.01 -t 0.4 -m Containment -@ 8 > out.paf
```

Output is [PAF](https://github.com/lh3/miniasm/blob/master/PAF.md) on stdout, with extra `k:i:`,
`J:f:`, `sh:f:` and similar tags carrying scores and diagnostics.

| flag | meaning |
|---|---|
| `-s`, `-p` | reference and reads, FASTA (gzip/bzip2/xz/zstd accepted) |
| `-k` | k-mer length (paper runs use 25) |
| `-r` | FracMinHash ratio — the fraction of k-mers sketched (0.01) |
| `-t` | homology threshold in [0,1] (0.4) |
| `-m` | scoring metric: `Containment`, `Jaccard`, `bucket_SH`, `bucket_LCS` |
| `-@` | mapping threads (default 1) |
| `-x` | write a JSON profiling report (`--profile-log`) |

**Choosing `-m`.** `Containment` (`intersection / m`) is the default and the right choice almost
always. `Jaccard` is stricter and ~24% slower, and it **collapses on high-error reads** — 6.5%
mapped on ONT against Containment's 42.9% — because its denominator grows when errors shrink the
intersection. `bucket_SH` skips refinement: ~16% faster, but it loses the precise scoring that
separates confident mappings from ambiguous ones.

## How it works

1. **Sketch** the reference and each read with FracMinHash, keeping k-mers whose hash falls below
   `r · u64::MAX`.
2. **Seed** — look up each read k-mer in the index, rarest first.
3. **Accumulate** hits into overlapping buckets, each two half-read-lengths wide, using a dense
   array sized by the read's own half-length.
4. **Prune** buckets by a seed-heuristic upper bound.
5. **Refine** survivors with a sliding window that maximises k-mer set overlap, and report the best.

This is set overlap, not alignment: no dynamic programming, no edit distance. Reported coordinates
are window boundaries.

## Documentation

**Start with one of these two.** [PORT_CHANGES.md](PORT_CHANGES.md) is what changed against the
C++ and why; [RESULTS.md](RESULTS.md) is what it measures. Everything else supports them.

| file | contents |
|---|---|
| [PORT_CHANGES.md](PORT_CHANGES.md) | **what makes it faster and lighter than the C++** — every optimization in one table, then each with verified C++ source snippets and the data structure that replaced it |
| [RESULTS.md](RESULTS.md) | **all benchmark numbers** — the single source, generated from `benchmarks/`; §11 is also the single home for approaches tried and rejected, with the measurement that rejected them |
| [charts](benchmarks/results/suite-1.0/x86_64/current/chart-index.html) | the profiling tables drawn as pie charts, regenerated with every result set ([aarch64](benchmarks/results/suite-1.0/aarch64/current/chart-index.html), [both hosts side by side](paper/generated/cross-arch/charts.html)) |
| [QUESTIONS.md](QUESTIONS.md) | the running log of what upstream asked, what was done, and what the benchmark said |
| [CONTRIBUTING.md](CONTRIBUTING.md) | how a PR is checked, and what decides a merge |
| [VERSIONING.md](VERSIONING.md) | the four versions, and the PR rule |
| [SECURITY.md](SECURITY.md) | why benchmarks run on a private host, and what gates them |
| [benchmarks/](benchmarks/) | the suite definition, runner, merge gate and result sets |
| [profiling/](profiling/) | how to profile a run, the probes behind the rejected optimizations, PAF validators, and measurements too costly to re-run |
| [simulate/](simulate/) | read simulators, error-rate sweep, and the PBSIM3 provenance scripts |
| [paper/](paper/) | tables and figures generated from a result set, for the paper |

## Correctness

`cargo test` runs 58 tests; run it in **both** profiles, since debug activates the `debug_assert`s
that guard the parallel index build and reader. Beyond that, changes are checked by byte-identical
PAF against the previous build on the whole human genome, by thread-count invariance, and by
`profiling/validate_paf.py`, which verifies structural, score and ground-truth invariants —
99.21% of simulated reads land within one read length of their true position (Containment; see
[RESULTS.md §7](RESULTS.md#7-correctness) for the other metrics).

That last check earns its keep: it found a bug byte-identical diffing structurally could not,
because both implementations shared it. See [RESULTS.md §7](RESULTS.md#7-correctness).

## Relationship to upstream

A faithful port, with a documented policy of fixing real bugs rather than reproducing them — each
divergence is commented at the site with what upstream does and why this differs. Output is not
bit-identical to the C++ and is not meant to be: shmap-rs uses a stable sort where upstream uses
`std::sort`, so tied buckets resolve deterministically here and arbitrarily there.
