# aarch64 — benchmark host `galaxy`

Every result set under this directory was measured here. Results are kept apart
by architecture because they are not comparable across one: `compare.py`
refuses to diff two result sets whose manifests name different hosts, and this
tree is that rule applied to the filesystem, so a pull request measured on
aarch64 is judged against aarch64.

## The machine

| | |
|---|---|
| architecture | `aarch64` (little endian) — what `uname -m` reports |
| CPU | ARM Neoverse-N1 |
| topology | 1 socket × 128 cores, 1 thread/core = **128 cores**, **1 NUMA node** |
| RAM | 246 GB |
| disk | 605 GB free on `/` (the corpus is ~46 GB) |
| OS | Ubuntu 25.10, kernel 6.17.0-22-generic |
| Rust | 1.97.1, host triple `aarch64-unknown-linux-gnu` — pinned by `rust-toolchain.toml` |
| C++ | g++ 15.2.0 |
| state during runs | `run.py` takes `~/.shmap-bench.lock`, so only one measurement runs at a time *on this host* |
| other tenants | shared machine, unlike a2. Measured draw during the 2026-08-09 run was ~0.12 of 128 cores (<0.1%). The raw load average reads ~4.7, which looks alarming and is not: almost all of it is uninterruptible-sleep tasks, which Linux counts in load but which consume no CPU. |

## Why this host is scientifically useful, not just a second data point

**It has one NUMA node.** a2 has four, and that single fact drove the whole of
QUESTIONS.md Q4 and Q5: thread scaling there flattens near 16 threads (one
socket) and degrades beyond it, and five attempts at per-node index replication
all measured as net regressions. Galaxy is the natural control for that
diagnosis.

**Measured 2026-08-09, commit `00d8c08`, and the control came back clean.**
Median speedup over all five benchmarks and three metrics, each host against
its own single-threaded time:

| | `-@2` | `-@4` | `-@8` | `-@16` | `-@32` | `-@64` |
|---|---:|---:|---:|---:|---:|---:|
| `x86_64` (a2, 4 nodes) | 1.6x | 2.7x | 4.3x | **6.0x** | 5.5x | 5.8x |
| `aarch64` (galaxy, 1 node) | 1.8x | 3.4x | 6.3x | 9.8x | 11.2x | **11.7x** |

a2 peaks at 16 threads — exactly one socket — and then goes *backwards*.
Galaxy climbs monotonically through 64 and has not yet turned over. Same
commit, same compiler, same corpus, and
[bit-identical counters](../../../paper/generated/cross-arch/) on both, so the
difference cannot be the code. **The Q4 conclusion is corroborated by hardware
rather than by argument**: the ceiling is cross-socket memory traffic, not the
pipeline and not the threading library.

It does not follow that the Q5 fix would have worked. Q5 failed for reasons
measured on a2 — `numa_balancing` migrating pages out from under the
placement, and mimalloc's eager arena commit defeating `set_mempolicy` — and
this host, having one node, cannot speak to either. What galaxy establishes is
that the *diagnosis* was right, not that the abandoned remedy was.

**It also has no AVX-512**, so the downclocking recorded in Q1's addendum
cannot apply. The shipped binary never contained AVX-512 on either host, but
this removes the possibility entirely.

## Comparability with x86_64

- **Same compiler.** `rust-toolchain.toml` pins 1.97.1 on both hosts. Without
  it this machine's fresh rustup installed 1.97.1, which would have made every
  cross-architecture number a comparison of two architectures *and* two
  compilers, with no way to attribute a difference to either.
- **Same corpus, byte for byte.** All six suite datasets were copied here and
  verified against the identity triple in `datasets.tsv`; `run.py` re-checks
  sizes before it measures anything.
- **Same output.** `cargo test --release` passes 58/58 here, *including*
  `golden_paf`, which compares against a PAF generated on x86_64. shmap-rs
  produces bit-identical output on both architectures, so any difference
  between these two trees is performance and nothing else.
- **Same thread sweep.** `suite.toml` sweeps `1..64` and stays that way here
  even though 128 cores are available, so the two hosts sweep identically. The
  extra 64 cores are a separate question, not a change to the shared matrix.

## This host is markedly quieter than a2

Indexing is an accidental but genuine noise-floor control. It does identical
work in all 15 rows of a run — same reference, same code, no dependence on the
read set — so the spread of `index_s` across those rows measures the *host*.

| | mean | min | max | spread | CV |
|---|---:|---:|---:|---:|---:|
| `x86_64` (a2) | 7.44s | 6.84 | 8.57 | 23.2% | 7.0% |
| `aarch64` (galaxy) | 6.53s | 6.47 | 6.89 | **6.4%** | **1.6%** |

Peak RSS over the same rows tells the same story: 14.3% spread on a2 against
3.3% here. So the shared host is the *steadier* of the two, which is the
opposite of what "shared" suggests and worth knowing before anyone discounts a
result measured here.

**Measured directly, too.** Commit `00d8c08` was run twice here on 2026-08-09,
seven hours apart — once for shmap-rs alone and once with the C++ reference —
which is a repeatability experiment rather than a proxy for one. The second
run's verdict was ACCEPT with no reviewable rows: whole-run wall time moved
−0.1%, +1.0%, +1.0%, +0.5%, −1.1% on the first five (benchmark, metric) pairs,
and the worst single thread count in the whole matrix was +5.2%. a2's rerun of
the same commit, by contrast, produced four rows between +3.2% and +6.5% and
single thread counts past +22%.

The practical consequence is for a2, not for this host: a2's own noise floor on
identical work is ±23%, which is larger than the 3–6% wall-time movements its
regression gate flags for review. A single a2 row moving a few percent is not
evidence of anything.

## The C++ reference

Built here from upstream at the pinned commit
[`63f1103`](https://github.com/pesho-ivanov/shmap/tree/63f1103a6e72394fada5f9d9726f4a38f739e8fa),
`-O3 -march=native -flto`, at the same path `suite.toml` already expects
(`~/Pesho/shmap/release/shmap`) — so no host-specific configuration.

`-DTRACY_ENABLE` was commented out of the upstream Makefile before building.
Upstream adds it unconditionally and it costs ~8.8%; `run.py` refuses a binary
carrying live Tracy symbols for exactly that reason. This build reports 3,
under the threshold of 10.

**Measured 2026-08-10.** shmap-rs is **1.94–2.64x** faster than it
single-threaded here (median 2.31x), against **1.69–2.77x** (median 2.45x) on
a2, and uses 2.55–2.66 GB against the C++'s 18.85 GB — 7.1x less. So the port's
advantage is a few percent smaller on ARM, in 12 of 15 rows.

Read that difference carefully. Both terms of this host's ratio were measured
in the same run, while a2's C++ rows date from 2026-08-01 and its shmap-rs rows
from 2026-08-09 — and a2's noise floor on identical work is 23%. The one row
where ARM comes out *ahead* by a wide margin (B04/bucket\_SH, 1.97x here
against 1.69x there) is also a2's lowest ratio in the whole table, which is
what a stale denominator would look like. A same-day C++ re-measurement on a2
would settle it; until then the cross-architecture speedup table marks that
column as approximate.

## What this host does not have

**No external-mapper corpus.** `suite.toml`'s `[external]` mappers — mapquik
and Winnowmap2 — are cached per host under `~/bench-refs`, and that directory
exists on a2 and not here. Two consequences, both visible in the result set
rather than hidden:

- `checks.tsv` carries no `concordance_mapquik` and no `concordance_winnowmap2`
  rows. a2's carries 12 and 15. Concordance against a mapper that was never run
  cannot be scored, so the checks are absent rather than passing.
- `paper/generated/aarch64/table_mapper_comparison` has no external rows. It
  used to show a2's, because the corpus was the one measurement directory the
  per-architecture split missed and `paper.py` read it unconditionally — so the
  table listed mappers this architecture's own checks record it never ran.

Build it here with `benchmarks/scripts/reference_mappers.py --run`, which takes
hours and needs the mapper binaries installed. Until then aarch64 has accuracy
evidence from B02's ground truth and agreement with the C++, but no
third-party concordance.

## Reproducing

```sh
python3 benchmarks/scripts/run.py --commit <sha>     # measure
python3 benchmarks/scripts/promote.py <result-set>   # adopt as the new baseline
```

`run.py` derives this directory from `uname -m`, so a run cannot file itself
under the wrong architecture.
