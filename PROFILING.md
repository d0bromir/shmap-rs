# shmap-rs optimization log

**What was changed, why, and what it measured at the time.** Benchmark numbers are not here — they
are in [`RESULTS.md`](RESULTS.md), generated from [`benchmarks/`](benchmarks/) on every run. Figures
below are the record of each individual change against the build it landed on, and are deliberately
*not* updated: their value is that they say what a specific change bought.

Instrumentation is `-x`/`--profile-log` (`src/profiling.rs`), which writes a per-run JSON report.
Every benchmark run archives those per result set in
`benchmarks/results/<suite>/<commit>/raw-profiles.tar.gz`, and RESULTS.md §5 is generated from them.

```sh
python3 benchmarks/scripts/run.py --commit <sha>          # the maintained runner
target/release/shmap -s ref.fa -p reads.fa -x --profile-log run.json   # one-off
```

## What's optimized

- **`match_rest`'s second sweep reuses the first's scores.** `map_read` sweeps `sorted_buckets`
  twice — once for the best mapping, once for the second-best that feeds mapq — and on the
  `Containment`/`Jaccard` path `find_best_mapping` is a pure function of the bucket location: it
  never reads `content` (which pruning mutates between the sweeps) and it restores `diff_hist`
  exactly as it found it, which is what `best_fixed_length`'s closing `debug_assert_eq!(intersection,
  0)` pins. So the second sweep was recomputing bit-identical scores. `RefineCache` records the
  first and replays it in the second; both walk the same slice in the same order, so replay is a
  monotone cursor over one reusable `Vec` with no hashing. Measured on real HG002 HiFi against the
  whole genome at `-@1`, **44% of `find_best_mapping` calls are eliminated at every depth**, and the
  effect is flat in coverage (1x / 3x / 10x): `match_rest_for_best2` **−66.4% / −67.1% / −66.5%**,
  `refine` **−39.2% / −41.3% / −39.8%**, `query_mapping` **−9.6% / −10.5% / −9.4%**. PAF output is
  byte-identical. `SHMAP_NO_REFINE_MEMO=1` disables it so one binary can A/B the change.
  New `refined_buckets`/`refine_memo_hits` counters close a gap the older reports had: `final_buckets`
  only counted buckets that *beat* the threshold, so nothing recorded what refining actually cost.
- **`h2single`/`h2multi` are sharded by k-mer hash, so the index build parallelises.**
  `index_initializing` was the serial floor of indexing: one thread doing ~31 M cache-missing
  hash-map inserts, ~8.4 s, and completely flat from 1 thread to 64 — ~80% of all indexing time at
  `-@ 32`. The map is now 8 shards; every occurrence of a hash lands in the same shard, so threads
  never touch the same k-mer and no locking is needed, and segments are still visited in file order
  within a shard so `max_matches` keeps the same hits. **8.4 s → 1.1-1.8 s.** Three things had to be
  right. *Shard on the low hash bits*: FracMinHash only keeps k-mers below `h_frac * u64::MAX`, so
  at `-r 0.01` every hash reaching the index is under `2^57` and the top 7 bits are always zero —
  sharding on the top bits (the usual advice) put every k-mer in shard 0 and silently serialised the
  build, one thread taking 6.96 s while the other 63 finished in 0.13 s, with output still correct.
  *Keep the interleaved fill at `-@ 1`*, where deferring the inserts loses the overlap the collector
  already had with the reader and sketcher (9.2 → 15.1 s). *Hold the shards in a fixed-size array*,
  not a `Vec`: every index probe in the mapping hot path goes through it, and the extra dependent
  load cost `collect_kmer_info` up to 24%. Eight shards rather than more because the insert phase
  bottoms out well before the shard count does, while a wider array is colder in the mapping path —
  at 10x `-@ 1`, 64 shards cost 3.4% overall against 8 shards' 0.7%.
- **The FASTA reader parses byte ranges in parallel, in two passes.** With the inserts sharded,
  reading became the floor: 4.3-5.6 s regardless of `-@`, ~70-80% of indexing. It is not I/O bound —
  the 3.18 GB reference streams in 0.87 s at 3.7 GB/s — so the cost is line splitting, newline
  stripping and copying. A first version split the file into 16 MB ranges parsed by up to 8 workers,
  but instrumenting it showed the win was capped by the collector: `recv_wait` 0.05 s against
  2.8-3.2 s spent concatenating ranges into a growing per-segment buffer on one thread — not the
  memcpy but the doubling reallocations and ~780 k serialised first-touch page faults. So pass 1
  now counts only, yielding every segment's exact size and every range's offset within it, and pass
  2 hands workers disjoint slices of an exactly-sized buffer to copy straight into. **4.4 s →
  1.5-1.7 s** (`fasta_scan` ~0.3 s, `fasta_fill` ~1.3 s), and peak RSS falls slightly since the
  growth reallocations are gone. Both passes drive one line-walker so they cannot disagree about
  line boundaries; two `debug_assert`s pin that pass 2 writes exactly what pass 1 counted and that a
  segment's parts tile its buffer with no gaps. Falls back to the serial reader for compressed
  input, small files, `-@ 1`, and non-Unix targets.
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
- **Indexing no longer has a single dominant phase, and is close to its floor.** The two serial
  phases above are gone: `index_initializing` 8.4 → 1.1-1.8 s, `index_reading` 4.4 → 1.5-1.7 s.
  Total indexing bottoms out near **2.9-3.4 s**, down from ~9.4 s, and no stage dominates — reading
  and the shard fill are each ~1.5 s, both within ~2x of the 0.87 s it takes merely to stream the
  3.18 GB reference off page cache. Further gains would have to come from *reading less* — a
  persistent on-disk index, or 2-bit packed sequence — rather than from parallelising harder. The
  software-prefetch idea (prefetch the bucket for k-mer `i + D` while inserting `i`, which would
  need `hashbrown` as a direct dependency for a hash-supplied raw entry API) is still open, but it
  now targets ~1.5 s rather than ~8 s. See RESULTS.md §3 for the per-thread breakdown.
- **Sketching** is ~2.0 ns/base on the benchmark host (6.3 s for the 3.12 Gbp reference at `-@1`,
  `allchr_real_24kbp-t1.profile.json`) and still latency-bound on the base-by-base rolling hash;
  the remaining big win would need SIMD or 2-bit-packed sequence encoding (large effort).

## How the bucket accumulator got here

Three rounds of fixing `Buckets`. An early sparse-`FxHashMap` rewrite — itself fixing a ~15 GB
dense-array blowup — regressed single-thread speed ~20% below the C++ in the `k=15` regime. An
append-only buffer merged once per read recovered most of that, and an O(n) radix sort recovered the
rest. The last round removed the merge entirely: a read produces ~4 M raw bucket contributions, but
the reference only *has* ~242 k buckets at that read's half-length, so shmap-rs now accumulates
straight into a dense, L3-resident array and reads the sorted result back in one linear scan. The
array that replaced the hash map is ~1.4 MB — this is not a return to the multi-GB dense array,
because it is sized by the read's own half-length rather than by the genome.
