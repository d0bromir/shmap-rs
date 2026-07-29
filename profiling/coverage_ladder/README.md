# Coverage ladder: 1x → 100x whole-genome

A scaling and soak test taking read depth an order of magnitude past anything
measured before: **311.7 Gbp of reads (100x of the human genome) in a single
run**. The question it answers is narrow and deliberate — *does anything
degrade at depth?* — and it is not a mapping-quality benchmark.

## Read carefully: what this is, and what it is not

The reads are **one 1.0000x whole-genome read set repeated N times**, streamed
through a FIFO. This was not a shortcut around available data, it is a hard
limit of the data that exists:

- The GIAB HG002 PacBio CCS 15 kb set that `../realworld_hifi/` was built from
  is 41.1 GB across all 18 SMRT cells — **~13x** of a 3.117 Gbp genome. 100x of
  *distinct* real HiFi reads is not in that dataset at any subsetting.
- Reaching 100x therefore requires either a different data source or repetition.

Consequences, stated plainly:

- `mapped%` and `mapq60%` being identical at every depth is **trivially
  expected** and is *not* evidence of quality holding up. Ignore those columns.
- What the repetition *does* validly measure: throughput, peak memory, per-read
  CPU cost, counter overflow, and per-read state isolation across 24.3 M reads.
  Those are properties of the implementation, not of the reads, and repetition
  does not weaken them.

The reads are also **simulated** (ground-truth-encoded headers), matched to the
HiFi length profile — 242,845 reads, 3,117,432,629 bases, mean 12,837 bp. Their
stage mix differs from real reads (see the caveat at the end).

## Setup

- **Reference**: T2T-CHM13v2.0 (`hs1.fa`), 3,117,292,070 bp, 25 segments.
- **Parameters**: `-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment` — the
  paper / Table-1 real-world parameters, same as `../realworld_hifi/`.
- **Method**: `wgs_ladder.sh` in this directory. Reads are `cat`ed N times into
  a FIFO; materializing the 100x point on disk would have cost ~312 GB. stdout
  goes to `/dev/null`, so this measures mapping throughput and not PAF
  writeout, identically at every depth.
- **Host**: 8-core WSL2 box, 13 GB RAM. **Not** the 64-core host every other
  file in `profiling/` was measured on — absolute times here are not comparable
  to `PROFILING.md` or `../realworld_hifi/`. The depth-to-depth *trends* are the
  result; the absolute numbers are not.
- **Binary**: includes the `refine` memo (`RefineCache`, `src/shmap/scoring.rs`).

## Results

| depth | thr | reads | read bases | wall | indexing | mapping | peak RSS | reads/s | Mbp/s | CPU µs/read |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1x | 1 | 242,845 | 3.1 Gbp | 61.4 s | 10.8 s | 50.6 s | 1.96 GB | 4,799 | 62 | 166.4 |
| 1x | 8 | 242,845 | 3.1 Gbp | 21.7 s | 9.8 s | 11.9 s | 2.13 GB | 20,378 | 262 | 265.6 |
| 10x | 8 | 2,428,450 | 31.2 Gbp | 114.8 s | 10.2 s | 104.6 s | 2.18 GB | 23,212 | 298 | 263.9 |
| 30x | 8 | 7,285,350 | 93.5 Gbp | 314.2 s | 10.0 s | 304.2 s | 2.16 GB | 23,950 | 307 | 261.8 |
| 100x | 8 | 24,284,500 | 311.7 Gbp | 1034.4 s | 9.9 s | 1024.5 s | **2.16 GB** | 23,703 | 304 | 269.0 |

`CPU µs/read` is the profiler's `query_mapping`, which sums across all workers,
divided by read count — i.e. total CPU, not wall time per read.

**Throughput is flat from 10x to 100x**: 23,212 → 23,950 → 23,703 reads/s, a
±1.5% band across a 10-fold increase in input. Per-read CPU cost is likewise
constant (263.9 / 261.8 / 269.0 µs).

**Peak memory is flat**: 2.13 → 2.16 GB from 1x to 100x. A **100-fold** increase
in input costs **1.4%** more resident memory. This extends the 1x→10x claim in
`../realworld_hifi/` by another order of magnitude, and is the strongest
available evidence that per-read state is genuinely bounded.

**Indexing is a fixed ~10 s** at every depth — 45% of wall at 1x, 1.0% at 100x.

Per-read work counters are *identical to two decimals* at every depth and both
thread counts:

| per read | 1x t1 | 1x t8 | 10x | 30x | 100x |
|---|---:|---:|---:|---:|---:|
| `kmers` | 127.46 | 127.46 | 127.46 | 127.46 | 127.46 |
| `kmers_unique` | 121.85 | 121.85 | 121.85 | 121.85 | 121.85 |
| `seeded_buckets` | 296.34 | 296.34 | 296.34 | 296.34 | 296.34 |
| `refined_buckets` | 2.74 | 2.74 | 2.74 | 2.74 | 2.74 |
| `refine_memo_hits` | 1.49 | 1.49 | 1.49 | 1.49 | 1.49 |
| `final_buckets` | 2.29 | 2.29 | 2.29 | 2.29 | 2.29 |

That is the real payoff of the repeated input: it makes these exactly
predictable, so any drift would be a bug. It confirms the per-read `Counters`
reset holds across 24.3 M reads — the C++ bug this port fixed (see
`src/shmap/mod.rs`'s module comment) would show up here as unbounded growth.
No counter overflowed: the largest, `possible_matches`, reached 1.74e12 against
an `i64` budget.

## The finding worth acting on: 8 cores buy 4.25x

Mapping throughput at `-@ 8` is only **4.25x** the `-@ 1` rate, and per-read CPU
cost rises from **166.4 µs at 1 thread to ~265 µs at 8** — the same work costs
60% more CPU per read once 8 workers run concurrently.

Nothing is being serialized: the per-read invariants above are identical, and
`indexing` is excluded. The workers are contending for memory. Each one streams
scattered ~4 KB windows of a multi-GB reference sketch and probes a ~700 MB
`h2single`, and eight of those saturate shared L3 and memory bandwidth.

This says the mapper is **memory-bound, not compute-bound**, at realistic thread
counts — which matches what two attempted optimizations found independently at
the micro scale (both were reverted; both failed because the per-read structures
are already cache-resident and the only cold traffic is the index probe). It
makes prefetching and index memory layout the lever that matters, and predicts
that instruction-level tuning of the per-read path will keep returning nothing.

## Caveat on stage mix

As a share of `query_mapping` at 100x: `match_seeds` 39.7%, `prepare` 19.6%
(`collect_kmer_info` 13.6%), `match_rest` 18.5% (`refine` 8.5%), `sketching`
15.0%, `bucket_merge` 6.9%. Stable across depth to within a point.

But this is **not** the real-read mix. On 2,000 real HiFi reads the same binary
gives `match_seeds` ~29% and `refine` ~19%. Simulated reads materially
under-weight `refine`. Use `../realworld_hifi/` for stage attribution and this
directory only for scaling behaviour.

## Files

- `results.csv` — the table above, machine-readable
- `wgs_{1,10,30,100}x-t{1,8}.profile` → `wgs_*.json` — full `-x` reports
- `time_*.txt` — `/usr/bin/time -v` records (wall, peak RSS)
- `wgs_ladder.sh` — the exact driver, including the FIFO streaming setup
- `cpp_comparison.md` — head-to-head against the C++ original (run on chr21;
  see that file for why the whole genome was not possible)
