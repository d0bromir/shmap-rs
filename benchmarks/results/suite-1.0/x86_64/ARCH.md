# x86_64 — benchmark host `a2`

Every result set under this directory was measured here. Results are kept apart
by architecture because they are not comparable across one: `compare.py`
refuses to diff two result sets whose manifests name different hosts, and this
tree is that rule applied to the filesystem, so a pull request measured on
x86_64 is judged against x86_64.

## The machine

| | |
|---|---|
| architecture | `x86_64` (little endian) — what `uname -m` reports |
| CPU | Intel Xeon Gold 5218 @ 2.30 GHz, Cascade Lake |
| topology | 4 sockets × 16 cores, 1 thread/core = **64 cores**, 4 NUMA nodes |
| L3 | 88 MiB (4 instances, one per socket) |
| RAM | 376 GB |
| OS | Ubuntu 24.04.3 LTS, kernel 6.8.0-100-generic |
| Rust | 1.97.1, host triple `x86_64-unknown-linux-gnu` |
| state during runs | idle and exclusively locked — `run.py` takes `~/.shmap-bench.lock` |

## Things measured here that are properties of *this* machine

Kept with the architecture rather than in `RESULTS.md`, because they explain
numbers that would look inexplicable on any other host.

- **4 NUMA nodes, and it shows.** Thread scaling flattens near 16 threads (one
  socket) and degrades past it. Per-read CPU cost rises continuously from the
  second thread — mildly within a socket, sharply across one. Diagnosed in
  QUESTIONS.md Q4; index replication was built and measured as a net regression
  in Q5, so the standing advice on this host is to cap `-@` at one socket, or
  pin with `numactl --cpunodebind=0 --membind=0`. Confirmed against hardware on
  2026-08-09: the same commit on `galaxy`, which has one NUMA node, scales
  monotonically to 11.7x at `-@64` while this host peaks at 6.0x on `-@16` and
  then declines. See `../aarch64/ARCH.md`.
- **AVX-512 downclocks the package.** Sustained AVX-512 across all 64 cores
  drops the clock 14–18% (2800 MHz scalar vs ~2300–2460 MHz), while a single
  busy core sees no penalty at all (3900 MHz either way). Measured in Q1's
  addendum. The shipped binary contains no AVX-512 — nothing in the build sets
  `target-cpu=native`, verified with `objdump` — so this only ever mattered to
  the standalone probes.
- **Single-core turbo is 3900 MHz**, which is the clock the per-base sketching
  figures in Q10 are expressed against.
- **The noise floor here is ±23% on identical work.** Indexing does the same
  work in all 15 rows of a run, so the spread of `index_s` across them measures
  the machine and not the code: mean 7.44s, min 6.84, max 8.57 — 23.2% spread,
  7.0% CV (2026-08-09, commit `00d8c08`). Peak RSS spreads 14.3% over the same
  rows. `galaxy` measured 6.4% and 3.3% for the same two quantities on the same
  commit, so this is a property of a2 rather than of the benchmark.

  This matters when reading a verdict. `suite.toml` flags a wall-time move
  above 3% for review, and that threshold sits *well inside* this machine's own
  noise on work that cannot have changed. Treat a few-percent movement on one
  row as unresolved rather than as a regression, and look at whether it tracks
  something physical — read count, thread count, a code path that actually
  changed — before believing it.

## Reproducing

```sh
python3 benchmarks/scripts/run.py --commit <sha>     # measure
python3 benchmarks/scripts/promote.py <result-set>   # adopt as the new baseline
```

`run.py` derives this directory from `uname -m`, so a run cannot file itself
under the wrong architecture.
