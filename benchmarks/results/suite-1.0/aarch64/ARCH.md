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

## Why this host is scientifically useful, not just a second data point

**It has one NUMA node.** a2 has four, and that single fact drove the whole of
QUESTIONS.md Q4 and Q5: thread scaling there flattens near 16 threads (one
socket) and degrades beyond it, and five attempts at per-node index replication
all measured as net regressions. Galaxy is the natural control for that
diagnosis. If thread scaling here stays clean well past 16, the Q4 conclusion —
that the ceiling is cross-socket memory traffic and not the pipeline — is
corroborated by hardware rather than by argument. If it does *not*, the
diagnosis needs revisiting.

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

## The C++ reference

Built here from upstream at the pinned commit
[`63f1103`](https://github.com/pesho-ivanov/shmap/tree/63f1103a6e72394fada5f9d9726f4a38f739e8fa),
`-O3 -march=native -flto`, at the same path `suite.toml` already expects
(`~/Pesho/shmap/release/shmap`) — so no host-specific configuration.

`-DTRACY_ENABLE` was commented out of the upstream Makefile before building.
Upstream adds it unconditionally and it costs ~8.8%; `run.py` refuses a binary
carrying live Tracy symbols for exactly that reason. This build reports 3,
under the threshold of 10.

## Reproducing

```sh
python3 benchmarks/scripts/run.py --commit <sha>     # measure
python3 benchmarks/scripts/promote.py <result-set>   # adopt as the new baseline
```

`run.py` derives this directory from `uname -m`, so a run cannot file itself
under the wrong architecture.
