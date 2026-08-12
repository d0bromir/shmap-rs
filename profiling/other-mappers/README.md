# Other mappers — stored measurements

Kept because re-running them is prohibitive, not because they are current. **These tools did not
change, so their numbers stand**, but they were measured at older parameters on an older build of
shmap-rs.

For a *maintained* comparison against Winnowmap2 and mapquik — rebuilt whenever the corpus or the
inputs change, and scored on every benchmark run — see [`../../RESULTS.md`](../../RESULTS.md) §8 and
[`../../benchmarks/scripts/reference_mappers.py`](../../benchmarks/).

| file | what |
|---|---|
| `table1_other_mappers.csv` | minimap2, Winnowmap2, BLEND, mapquik, minSHmap, C++ `shmap` on four datasets — mapped-at-q60, wrong-at-q60, index/map seconds, memory |
| `wgs_k15_shmap_cpp_py.csv` | shmap-rs / C++ / Python at `k=15` on HiFi, ONT and CLR, whole-genome and chr21 |

**Why these are not re-run.** Winnowmap2 took **28 688 s — nearly 8 hours — for a single row** of
`table1_other_mappers.csv`, and 8 041 s for another. That is the whole justification for the cached
corpus design: measure once, join against it thereafter.

---

## Reading `table1_other_mappers.csv`

Single-threaded, 64-core AVX-512 host, Table-1 parameters
(`-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment`).

- **Accuracy matched the C++ original exactly** — same correct-q60 on every dataset, zero wrong —
  while running 2.8-3.1x faster single-threaded on the two small sets and using ~8x less memory
  (2.4 GB against 18.9 GB). On the deep real-HiFi workload the speedup settles at **1.89-2.04x**;
  the higher ratio on the small sets is partly fixed C++ startup cost amortising away, so the
  lower figure is the one to quote for realistic whole-genome runs.
- **Fastest of the correct mappers** single-threaded: on real HG002 reads, 11.7 s against 84 s
  (BLEND), 150 s (minimap2), 356 s (Winnowmap2).
- shmap and shmap-rs **trade recall for speed** on the low-similarity chrY sets. Winnowmap2 maps
  more but is 100-380x slower and far heavier.

**mapquik reads 0 mapped in every row, and that is an artefact — not a property of the mapper.**
It counts newline characters as bases, so a line-wrapped reference yields coordinates in file-offset
space. `hs1.fa` is wrapped at 50 columns, which is exactly the 1.02x inflation seen in its reported
chromosome lengths. Given a one-line reference it maps ~99% of reads and agrees with shmap-rs on
96-98% of placements. The maintained corpus passes it a one-line reference; see RESULTS.md §8.

**mapquik's `--nosimd` switch is not the same tool as its default**, which is why it is absent from
the aarch64 corpus rather than built that way. Its k-min-mer crate compiles an AVX-512-only ntHash
iterator unconditionally, so it cannot be built on ARM at all. Measured on a2 (B01, 148 225 reads):
two runs of the identical default command agree on **100.00%** of records, while default against
`--nosimd` differs on 39 701 of 148 224 — **26.8%** — and drops one read entirely. The control being
exact is what makes that conclusive. See the commentary on `[external.mapquik]` in `suite.toml`.

## Reading `wgs_k15_shmap_cpp_py.csv`

Real HG002 long reads (6 000 each) against the whole T2T-CHM13 genome at the minSHmap benchmark's
parameters: `k=15`, `r = 2/(w+1) = 0.0625`, `-m Containment`, dataset-specific `theta`, 4 threads.

This is **shmap's hardest regime**: 15-mers are hugely repetitive genome-wide, so the index is far
denser and pruning is far weaker than at `k=25`. It is kept because it is the only stored comparison
in that regime, and because `[params.ont-k15]` in `suite.toml` targets the same `k` for high-error
reads — RESULTS.md §9 shows why (`(1-e)^k > t` is the operating envelope, and lowering `k` is one of
the two ways to widen it).

The C++ column is single-threaded by necessity: upstream has no threading.
