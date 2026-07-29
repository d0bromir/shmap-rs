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
| HiFi (12.8 kbp reads) | 1995.6 s | **725.6 s** | **2.75x** | 7.67 GB | **7.07 GB** | -8% |
| ONT (35.4 kbp reads) | 4782.5 s | **1932.7 s** | **2.47x** | 10.96 GB | **7.25 GB** | **-34%** |
| CLR (3.1 kbp reads) | 707.2 s | **491.8 s** | **1.44x** | 7.87 GB | **7.20 GB** | -8% |

PAF output is byte-identical on all three (0 differing lines; HiFi 5991 mapped / 5689 Q60, ONT 5750
/ 5227, CLR 294 / 216 in both builds), so this is pure throughput — the "never degrade mapping"
gate holds.

Stage timers, which are the whole story:

| dataset | `bucket_merge` | `match_seeds` | `indexing` |
|---|---:|---:|---:|
| HiFi before → after | 1342.1 → **39.1 s** | 618.2 → 648.2 s | 21.0 → 23.1 s |
| ONT before → after | 2784.4 → **62.2 s** | 1278.1 → 1155.3 s | 21.0 → 22.6 s |
| CLR before → after | 305.0 → **64.0 s** | 319.6 → 339.9 s | 20.8 → 21.7 s |

Two things worth reading off this:

- **`bucket_merge` lands at 39-64 s on every platform**, from 305-2784 s. The dense accumulator's
  cost depends on the size of the bucket space — a property of the reference and the read's
  half-length — not on how many contributions are poured into it, so it stops scaling with
  repetitiveness. The three speedups differ only because they track how `bucket_merge`-dominated
  each baseline was (67% / 58% / 43% of wall), not anything about the datasets.
- **`match_seeds` is essentially untouched and is now the whole of mapping** (92% / 61% / 73%).
  The win came from deleting work, not from making the remaining work faster. That is the next
  target, and it is bounded by raw hit volume — `max_seed_matches` peaked at 11.2 M for a single
  seed on HiFi.

ONT's -34% RSS is the largest memory win and has the same cause: its long reads produced the
biggest per-read contribution buffers, and those `Vec`s (grown and never shrunk across reads, plus
the radix ping-pong copy) are gone entirely, replaced by a ~1.4 MB dense array.

That memory win compounds with thread count, which is the part that matters most. Those buffers
were **per worker**, so the old design's footprint grew with `-@`: ONT went 10.96 GB at `-@ 1` to
**22.46 GB at `-@ 4`**. The dense array is ~1.4 MB per worker, so the new build is essentially flat
(7.25 GB -> 9.06 GB over the same range, and that delta is index build-up, not per-worker state).
At `-@ 4` the three platforms are 3.06x / 3.00x / 1.57x faster than the baseline, with ONT's peak
RSS down 60%. This removes a scaling hazard rather than a constant factor — the old design would
have kept growing at 8, 16 and 32 threads.

`indexing` is the one line that got slightly slower, by ~1-2 s: `h2multi`'s big hit lists are shrunk
to fit once built, which at k=15 copies GBs of hits, and that is where much of the RSS drop comes
from.

### Table-1 datasets

Regenerated with the same discipline: `benchmark.py --datasets all --threads N --profile --only
shmap-rs`, both columns measured back-to-back on the same idle host. The raw reports are
`profiling/*.profile.json` (dumped into `profiling/tables.md`); the previous generation is kept
under `profiling/old/`.

`-@ 1`:

| dataset | before | after | | RSS before | RSS after |
|---|---:|---:|---:|---:|---:|
| chrY_sim_10kbp_10x | 57.7 s | **35.9 s** | **1.61x** | 0.19 GB | **0.13 GB** |
| allchr_sim_10kbp_1x | 89.9 s | **61.7 s** | **1.46x** | 2.73 GB | **2.41 GB** |
| chrY_sim_24kbp_10x | 16.7 s | **11.1 s** | **1.50x** | 0.19 GB | **0.13 GB** |
| allchr_real_24kbp | 11.8 s | 11.7 s | 1.01x | 2.73 GB | **2.36 GB** |

`-@ 16`:

| dataset | before | after | | RSS before | RSS after |
|---|---:|---:|---:|---:|---:|
| chrY_sim_10kbp_10x | 4.7 s | **2.9 s** | **1.62x** | 0.19 GB | **0.13 GB** |
| allchr_sim_10kbp_1x | 17.4 s | **15.9 s** | **1.09x** | 2.02 GB | 2.39 GB |
| chrY_sim_24kbp_10x | 1.9 s | **1.2 s** | **1.58x** | 0.19 GB | **0.13 GB** |
| allchr_real_24kbp | 10.8 s | 10.7 s | 1.01x | 2.02 GB | 2.03 GB |

Accuracy is unchanged everywhere (Mapped Q60 22918 / 228165 / 6902 / 1876, Wrong Q60 = 0).
`allchr_real_24kbp` is ~90% indexing, so it barely moves on time and gains only memory. The one
memory *increase* is `allchr_sim_10kbp_1x` at `-@ 16` (2.02 -> 2.39 GB). Its 10 kbp reads give a
half-length of ~127, so the bucket space is ~246 k slots and each worker's dense accumulator is
~3.9 MB — ~63 MB across 16 workers. That is a contributor rather than the whole 370 MB, which has
not been attributed further; it is bounded by `MAX_DENSE_SLOTS` either way, and it is the price of
the mapping win everywhere else.

> Regenerating this table is what caught a real regression, and it is worth recording why the WGS
> numbers alone would not have. The dense accumulator originally re-zeroed the whole array and
> re-scanned it per read — both O(bucket space), which is invisible across 6 000 whole-genome reads
> and crippling across 242 845 short ones: `allchr_sim_10kbp_1x` had gone **82 s -> 168 s**, a 2x
> regression, with 50 s of it pure `memset`. The fix is in `Buckets`: the array is never re-zeroed
> (extraction already empties every slot it takes), and extraction picks per read between an
> in-order scan and a sorted walk of just the touched slots, depending on what fraction of the
> bucket space the read actually touched. Both paths are pinned against the sparse path by
> `dense_and_sparse_paths_agree`.

### Real HiFi WGS at 1x / 3x / 10x

The first run against **real** whole-genome long-read data at meaningful depth (everything else
here uses 6 000-read subsets or simulated reads). 0.24-2.4 M real HG002 PacBio CCS reads vs
T2T-CHM13, paper parameters (`-k 25 -r 0.01 -t 0.4`), single-threaded. Full write-up and raw
reports in `profiling/realworld_hifi/`.

Inputs — reference `hs1.fa`, 3.18 GB, 3 117 292 070 bp, 25 segments:

| reads file | size | reads | read bases | read len mean/min/max | coverage of hs1 |
|---|---:|---:|---:|---:|---:|
| `hifi_1x.fa` | 3.12 GB | 242 534 | 3 113 721 004 | 12 838 / 114 / 17 603 bp | 0.9989x |
| `hifi_3x.fa` | 9.36 GB | 727 602 | 9 336 852 851 | 12 832 / 114 / 18 106 bp | 2.9952x |
| `hifi_10x.fa` | 31.22 GB | 2 425 341 | 31 132 446 551 | 12 836 / 62 / 18 314 bp | 9.9870x |

Invoked as `shmap -s hs1.fa -p hifi_${N}x.fa -k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment`,
plus `-@ 1 -x --profile-log rs_${N}x.profile.json` for shmap-rs (the C++ has no threading flag).

| depth | shmap-rs | C++ shmap | speedup | shmap-rs RSS | C++ RSS |
|---|---:|---:|---:|---:|---:|
| 1x | **66.9 s** | 108.6 s | **1.62x** | **2.11 GB** | 19.30 GB |
| 3x | **177.8 s** | 264.2 s | **1.49x** | **2.35 GB** | 19.30 GB |
| 10x | **566.1 s** | 810.7 s | **1.43x** | **2.54 GB** | 19.31 GB |

Per-read cost is constant across depths (0.232 / 0.230 / 0.229 ms) and memory is nearly flat
(2.11 -> 2.54 GB for 10x the reads), so throughput scales linearly and nothing degrades at depth.

**This workload's profile is nothing like the k=15 stress regime**, and that matters for what to
do next. Splitting per-read work at 10x: `match_rest` 31.9% (of which `refine` alone is 114.5 s),
`prepare` 26.0% (of which `collect_kmer_info` is 88.9 s), `match_seeds` 25.9%, `sketching` 12.3%
— and **`bucket_merge` just 3.6%**. The dense accumulator cut `bucket_merge` 36x and that is where
almost all of the k=15 win came from, but it is nearly irrelevant to real-world k=25 use. The
measured next targets are `refine`, `collect_kmer_info` and `match_seeds`, in that order.

## What's optimized

- **`Buckets` storage → append-only `Vec` + LSD radix sort**, not a hashmap. *(Superseded as the
  primary path by the dense accumulator below; this is what that replaced, and it survives as the
  bounded-memory fallback.)* An intermediate sparse
  `FxHashMap<BucketLoc, BucketContent>` design (replacing a whole-reference-sized `Vec`, ~15 GB per
  worker thread on the human genome) fixed a memory blowup but made single-thread mapping ~20%
  *slower* than the C++ original on k=15 whole-genome reads: every touch was a full hashmap
  `entry()` (hash + probe + possible resize), and a read there can touch millions of buckets.
  `add_to_pos`/`add_to_bucket` now just push onto a flat `Vec` (no hash), and duplicate locations
  are merged once per read via a 4-pass-max LSD radix sort on a packed `(segm_id, b)` key — the
  pass count is computed per read (skip always-zero high bits) rather than fixed, since `b` is
  usually far smaller than its 32-bit budget. Net at the time: **1.6× faster than the hashmap
  regression, and 25% faster than the C++ original** on WGS k=15 HiFi `-@1` (1972.7s vs 2637.2s),
  same memory order of magnitude, byte-identical mapped/mapq.
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
  read-modify-write into a dense array. Global bucket ids are ordered by `(segm_id, b)`, so the
  sorted+deduplicated result needs no sort at all: extraction either scans the array in order or
  walks a sorted list of just the slots this read touched, whichever is cheaper for the read in
  hand (see the Table-1 note above for why both are needed). On the full 6 000-read HiFi WGS run
  at `-@1`, **`bucket_merge` 1342.1 s → 39.1 s and wall 1995.6 s → 725.6 s**, byte-identical
  output. This is not the old dense array that caused a ~15 GB blowup: that one was sized by reference length over `MIN_HALFLEN`,
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
  memory. They are read-only after indexing, so the final sort pass also shrinks them — but only
  where the slack is worth the copy (`> len/8 + 8`): shrinking the two- and three-hit lists that
  dominate by *count* was ~5.8 M reallocations for ~100 MB and measured a net loss, while the rare
  very-high-frequency k-mers that dominate by *bytes* clear the threshold easily.
- **`lto = "fat"` + `codegen-units = 1`.** The release profile was previously the Cargo default, so
  the hot loops never got inlined across the `needletail`/`rustc-hash` boundary or across the 16
  default codegen units. Worth ~5% wall on its own. `panic` stays at `unwind`: `map_reads` uses
  `catch_unwind` to isolate a panicking read, so `abort` would change behavior, not just codegen.

## Remaining bottlenecks

- **`match_seeds` is now the whole of mapping** on k=15 whole-genome reads — 92% on HiFi, 73% on
  CLR, 61% on ONT, since `bucket_merge` collapsed to a near-constant 39-64 s. It is bounded by
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
- **`index_initializing` is now the serial floor of indexing.** On the k=25 whole genome it is one
  thread performing ~31 M hash-map insert operations (one per sketched k-mer) that settle into
  ~19 M distinct entries, in a table far larger than cache: 7.25 s, or ~230 ns per insert
  (`allchr_real_24kbp-t1.profile.json`). That is almost entirely cache/TLB miss latency rather than
  instruction count, which is why it barely moved from any of the work above. The two levers that would actually bite: (a) *shard* `h2single`/`h2multi` by hash across
  `-@` threads, which is compatible with determinism because every occurrence of a given hash lands
  in the same shard and shards would still see segments in file order (this is the big one — it
  turns the phase from serial into parallel); (b) software-prefetch the bucket for k-mer `i + D`
  while inserting `i`, which needs `hashbrown` as a direct dependency for a hash-supplied raw
  entry API, since `std`'s `HashMap` exposes no bucket addresses.
- **Sketching** is ~2.0 ns/base on the benchmark host (6.3 s for the 3.12 Gbp reference at `-@1`,
  `allchr_real_24kbp-t1.profile.json`) and still latency-bound on the base-by-base rolling hash;
  the remaining big win would need SIMD or 2-bit-packed sequence encoding (large effort).
