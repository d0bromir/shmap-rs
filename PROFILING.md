# shmap-rs profiling

Instrumentation: `-x`/`--profile` (`src/profiling.rs`), writing a per-run JSON report. Reproduce:

```
python3 profiling/benchmark.py --datasets all --threads 16 --profile --only shmap-rs
# or directly:
shmap -s ref.fa -p reads.fa -k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment \
    -@ 16 -x --profile-log run.profile.json
```

`profiling/tables.md` (via `profiling/extract_tables.py`) is a full raw dump of every report;
this file is just the summary.

## Current numbers

### WGS long reads (the regime the mapper is slowest in)

6 000 real HG002 reads per platform vs the whole T2T-CHM13 genome, `-k 15 -r 0.0625 -m Containment
-@ 1` with the benchmark's per-platform `theta` (HiFi 0.20, ONT 0.15, CLR 0.18), on the 64-core
benchmark host. Runs were sequential on an idle machine, timed end to end with `/usr/bin/time -v`.
"before" is commit `1de2a54`.

| dataset | wall before | wall after | | RSS before | RSS after | |
|---|---:|---:|---:|---:|---:|---:|
| HiFi (12.8 kbp reads) | 1995.6 s | **685.9 s** | **2.91x** | 7.67 GB | **7.06 GB** | -8% |
| ONT (35.4 kbp reads) | 4782.5 s | **1846.6 s** | **2.59x** | 10.96 GB | **7.26 GB** | **-34%** |
| CLR (3.1 kbp reads) | 707.2 s | **473.6 s** | **1.49x** | 7.87 GB | **7.20 GB** | -8% |

PAF output is byte-identical on all three (0 differing lines; HiFi 5991 mapped / 5689 Q60, ONT 5750
/ 5227, CLR 294 / 216 in both builds), so this is pure throughput — the "never degrade mapping"
gate holds.

Stage timers, which are the whole story:

| dataset | `bucket_merge` | `match_seeds` | `indexing` |
|---|---:|---:|---:|
| HiFi before → after | 1342.1 → **36.6 s** | 618.2 → 610.0 s | 21.0 → 23.1 s |
| ONT before → after | 2784.4 → **58.3 s** | 1278.1 → 1076.6 s | 21.0 → 22.4 s |
| CLR before → after | 305.0 → **59.9 s** | 319.6 → 328.5 s | 20.8 → 21.6 s |

Two things worth reading off this:

- **`bucket_merge` lands at 37-60 s on every platform**, from 305-2784 s. The dense accumulator's
  cost depends on the size of the bucket space — a property of the reference and the read's
  half-length — not on how many contributions are poured into it, so it stops scaling with
  repetitiveness. The three speedups differ only because they track how `bucket_merge`-dominated
  each baseline was (67% / 58% / 43% of wall), not anything about the datasets.
- **`match_seeds` is essentially untouched and is now the whole of mapping** (89% / 59% / 73%).
  The win came from deleting work, not from making the remaining work faster. That is the next
  target, and it is bounded by raw hit volume — `max_seed_matches` peaked at 11.2 M for a single
  seed on HiFi.

ONT's -34% RSS is the largest memory win and has the same cause: its long reads produced the
biggest per-read contribution buffers, and those `Vec`s (grown and never shrunk across reads, plus
the radix ping-pong copy) are gone entirely, replaced by a ~1.4 MB dense array.

`indexing` is the one line that got slightly slower, by ~1-2 s: `h2multi`'s big hit lists are shrunk
to fit once built, which at k=15 copies GBs of hits, and that is where much of the RSS drop comes
from.

### Table-1 datasets

> **Stale.** This table predates the bucket-accumulator and indexing work below and has not been
> re-run; use `profiling/benchmark.py --datasets all --threads 16 --profile --only shmap-rs` to
> refresh it. Spot-checked on `allchr_real_24kbp` (2 000 reads, k=25), where output is unchanged
> and the effect is small because that run is ~90% indexing, not mapping: `-@1` 11.66 s → 11.78 s
> (+1%) at 2.86 GB → 2.56 GB (−11%), and `-@8` 11.8-13.5 s → 10.6-10.9 s (−9%) at equal memory.

| Dataset | Threads | Wall | Peak RSS |
|---|---:|---:|---:|
| chrY_sim_10kbp_10x  |  1 |  73.5s | 0.19 GB |
| chrY_sim_10kbp_10x  | 16 |   5.6s | 0.19 GB |
| chrY_sim_24kbp_10x  |  1 |  19.2s | 0.19 GB |
| chrY_sim_24kbp_10x  | 16 |   2.0s | 0.19 GB |
| allchr_sim_10kbp_1x |  1 |  81.3s | 2.73 GB |
| allchr_sim_10kbp_1x | 16 |  14.8s | 2.44 GB |
| allchr_real_24kbp   |  1 |  10.6s | 2.73 GB |
| allchr_real_24kbp   | 16 |   9.6s | 2.02 GB |

Accuracy identical at every thread count (Mapped Q60: 22918 / 6902 / 228165 / 1876, Wrong Q60 = 0).

## What's optimized

- **`Buckets` storage → append-only `Vec` + LSD radix sort**, not a hashmap. An intermediate sparse
  `FxHashMap<BucketLoc, BucketContent>` design (replacing a whole-reference-sized `Vec`, ~15 GB per
  worker thread on the human genome) fixed a memory blowup but made single-thread mapping ~20%
  *slower* than the C++ original on k=15 whole-genome reads: every touch was a full hashmap
  `entry()` (hash + probe + possible resize), and a read there can touch millions of buckets.
  `add_to_pos`/`add_to_bucket` now just push onto a flat `Vec` (no hash), and duplicate locations
  are merged once per read via a 4-pass-max LSD radix sort on a packed `(segm_id, b)` key — the
  pass count is computed per read (skip always-zero high bits) rather than fixed, since `b` is
  usually far smaller than its 32-bit budget. Net: **1.6× faster than the hashmap regression, and
  now 25% faster than the C++ original** on WGS k=15 HiFi `-@1` (1972.7s vs 2637.2s), same memory
  order of magnitude (7.5 GB vs 13.5 GB), byte-identical mapped/mapq.
- **Buffered stdout** in the collector instead of `print!()` per read.
- **`match_seeds` streams multi-hit seeds** into buckets directly (sorted hits → monotonic bucket
  index) instead of a per-seed scratch hashmap + per-hit division.
- **Bucket accumulation is a dense array, not a sort.** This is the big one for WGS. Measured on
  whole-genome k=15 HiFi at `-@1`, a read produced ~4.0 M raw bucket contributions but only ~242 k
  *distinct* buckets, and collapsing the former to the latter by radix-sorting 32-byte records
  moved ~1.1 GB per read at memory-bandwidth speed — `bucket_merge` alone was 56% of total wall.
  The fix is to stop materializing contributions at all: the whole reference only contains
  `reference_sketch_len / halflen` buckets (~242 k here, ~4 MB of 16-byte slots, L3-resident) and
  such a read touches nearly all of them, so `add_to_pos`/`add_to_bucket` became one indexed
  read-modify-write into a dense array, and the sorted+deduplicated result comes back from a single
  linear scan (global bucket ids are ordered by `(segm_id, b)`, so the scan *is* the sort).
  **`bucket_merge` 21.9 s → 0.6 s; mapping −65%**, byte-identical output. This is not the old dense
  array that caused a ~15 GB blowup: that one was sized by reference length over `MIN_HALFLEN`,
  this one by reference length over the *read's own* half-length, and it refuses to allocate past
  `MAX_DENSE_SLOTS` (~32 MB/thread), falling back to the sparse radix path — kept, and pinned by a
  test asserting the two paths agree exactly.
- **Sparse-path fixes** (still live for the short-read fallback, and each measured on its own before
  the dense path landed): the radix key packed `segm_id << 32`, forcing a 37-bit key and a third
  O(n) pass where the real key is ~22 bits — measuring the width per read cut it to two passes
  (−30%); the sorted entry shrank 32 → 24 bytes by hoisting `BucketContent`'s `i`/`seeds`, which
  are uniform across a read, out of the per-entry payload; all passes' histograms are built in one
  counting scan instead of one per pass (a histogram is permutation-invariant); the key-width
  maxima are tracked at push time instead of by a full scan; and the final by-`matches` ordering
  sorts 8-byte packed keys rather than 32-byte records.
- **`match_seeds`'s per-seed scratch is two fixed slots**, not a `Vec` with insert/remove. A hit at
  bucket `b` touches only `b` and `b - 1` and `b` never decreases within a segment, so the live
  window is provably two buckets wide.
- **Reference indexing parallelized** across `-@`, applied in strict file order for determinism.
- **Sketching**: precomputed rolling-hash LUTs (fewer rotates/base) + `Entry` API in k-mer
  indexing (hash once, not twice).
- **Sketching hot loop is bounds-check free.** The rolling update indexed `s[r]` and `s[r - k]` by
  a signed `RPos`, so every base of the reference paid two bounds checks plus sign-extension, and
  the mid-loop `r >= s.len()` break stopped LLVM treating it as a counted loop at all. Walking the
  incoming/outgoing bases as a pair of zipped slice iterators removes all of it: **~13-17% off
  `index_sketching`**. Interleaving the fw/rc LUT pairs into one `[Hash; 2]` table to halve the
  per-base load count was also tried and measured ~6% *slower* (the 16-byte load round-trips
  through a vector register), so the tables stay separate.
- **Sketching a segment is split into chunks** rather than one segment per worker. A k-mer window
  depends only on the `k` bases under it, so `sketch_slice_into` sketches a window range at an
  offset and the pieces concatenate to exactly the serial sketch (pinned by a unit test over many
  split points). This removes the Amdahl cap where the phase could not finish faster than the
  single longest chromosome — the reason `-@16` indexed barely faster than `-@1` on
  `allchr_real_24kbp`. **~18% off `indexing` at `-@8`** on the synthetic reference.
- **FASTA records are handed to the caller by value.** `read_fasta` yielded `&[u8]` and *both*
  call sites immediately did `seq.to_vec()` — a second full copy of every chromosome, and a second
  chromosome-sized live allocation. needletail already returns an owned, newline-stripped buffer,
  so moving it through costs nothing. The per-record header `to_vec()` and `Cow` allocation are
  gone too (one reused `String`).
- **Sequence read-ahead is bounded by segments, not by `threads * 4` segments.** All chunks of a
  segment share one `Arc`'d copy of its bases, and that `Arc` is deliberately not carried into the
  completion message, so a segment's bases are freed as soon as it is sketched instead of being
  held alive across the much slower `add_segment`. **~11-13% off peak RSS** on indexing-dominated
  runs.
- **Sketch buffers are sized from the binomial, not a flat `1.1 *`.** Selection is a Bernoulli
  trial per k-mer, so mean + 6σ is ~0.5% of slack on a chromosome instead of 10%, with overflow
  merely slow (never wrong).
- **`h2multi` lists start at capacity 2 and are shrunk once built.** A k-mer that reaches `h2multi`
  always ends up with at least two hits, so starting empty meant allocating at capacity 1 and
  immediately reallocating — millions of avoidable reallocations per genome, for identical final
  memory. They are read-only after indexing, so the final sort pass also shrinks them to fit.
- **`lto = "fat"` + `codegen-units = 1`.** The release profile was previously the Cargo default, so
  the hot loops never got inlined across the `needletail`/`rustc-hash` boundary or across the 16
  default codegen units. Worth ~5% wall on its own. `panic` stays at `unwind`: `map_reads` uses
  `catch_unwind` to isolate a panicking read, so `abort` would change behavior, not just codegen.

## Remaining bottlenecks

- **`match_seeds` is now the whole of mapping** on k=15 whole-genome reads — 89% on HiFi, 73% on
  CLR, 59% on ONT, since `bucket_merge` collapsed to a near-constant 37-60 s. It is bounded by
  sheer hit volume: a handful of
  very-repetitive 15-mers can each have millions of hits genome-wide (`max_seed_matches` peaked at
  11.2 M for a single seed), and every one of them is read from `h2multi` and folded into a bucket.
  Reducing *volume* is the only remaining lever, and it is the one place where that has been
  investigated and **rejected**: `-M`/`--max_matches` (blacklisting over-frequent k-mers at index
  time) measurably shifted the mapq distribution even at a mild threshold, and an aggressive one
  dropped reads from mapped entirely (300/300 → 279/300 on a test sample). Not safe as a default;
  the "never degrade mapping" gate rules it out unless a future change can bound volume without
  touching results. The per-hit work itself is now down to an indexed accumulate, so the honest
  next step is measuring whether it is bound by streaming `h2multi` or by the per-hit arithmetic.
- **`index_initializing` is now the serial floor of indexing.** It is one thread doing ~19M
  `h2single` inserts into a table far larger than cache, and at ~175-210 ns per k-mer it is almost
  entirely cache/TLB miss latency, not instruction count — it barely moved from any of the work
  above. The two levers that would actually bite: (a) *shard* `h2single`/`h2multi` by hash across
  `-@` threads, which is compatible with determinism because every occurrence of a given hash lands
  in the same shard and shards would still see segments in file order (this is the big one — it
  turns the phase from serial into parallel); (b) software-prefetch the bucket for k-mer `i + D`
  while inserting `i`, which needs `hashbrown` as a direct dependency for a hash-supplied raw
  entry API, since `std`'s `HashMap` exposes no bucket addresses.
- **Sketching** is now ~1.7 ns/base and still latency-bound on the base-by-base rolling hash; the
  remaining big win would need SIMD or 2-bit-packed sequence encoding (large effort).
