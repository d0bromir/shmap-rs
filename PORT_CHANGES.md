# Single-thread speed and memory: what changed vs the C++ `shmap` and the map-shmap paper

**Scope.** Specifically the changes that make one thread run faster or use less memory than the
C++ reference implementation and the paper's abstract algorithm (Ivanov & Medvedev, *map-shmap:
Practical long-read mapping with seed heuristic on sketches*) would predict. Not covered here,
deliberately: correctness fixes (they change output, not cost — a separate question), the
multithreading capability itself (a purely additive capability on top of what's below — `-@1`
pays none of its overhead and gains none of its benefit), and new CLI-visible features. A short
closing section says where each of those lives instead.

**Why single-thread specifically.** It's the fair comparison — the C++ has no threading at all,
so `-@1` is the only column measured against it on equal terms (RESULTS.md §2). It's also the
number a per-worker cost multiplies from: several changes below live inside `Buckets`, one
instance per worker thread, so what one thread costs is what every additional thread costs again.

**The headline numbers these changes produce**, across five benchmarks and three metrics
(RESULTS.md §1–2): **1.91–2.74x** faster wall-clock than the C++, single-threaded, and **6.90–
7.43x less peak memory** (2.54–2.73 GB vs the C++'s constant 18.85 GB). Two largely independent
stories explain them — one data structure whose peak size fell by roughly three orders of
magnitude accounts for nearly all of the memory result, and about ten smaller techniques,
individually worth single- to double-digit percentages, add up to the speed result.

**Methodology and its limit.** C++ comparisons come from the Rust source's own doc comments,
written against the C++ source at port time and confirmed by grep-based call-site audits (see
`docs/sections/16_defects.tex`'s methodology note). The C++ *source* isn't available on this host
— only its release binary — so where a comment makes no C++ claim, this document invents none.
Numbers not attributed to RESULTS.md come from `PROFILING.md`, the chronological engineering log
that records exact before/after figures at the time each change landed and is deliberately never
updated — cited here rather than re-derived, reorganized around *why*, not *when*. For the base
algorithm's own definitions, see `docs/shmap_algorithm.pdf` (chapters cited as, e.g., §8
Bucketing).

---

## Part A — Memory: from a C++-competitive footprint to 6.9–7.4x smaller

### A.1 The bucket accumulator: three generations, ~15 GB down to ~4 MB per worker

This single data structure is nearly the whole memory story. `Buckets` (§8 Bucketing) accumulates
per-bucket match counts and coordinate ranges while a read is scored against candidate reference
windows — one live instance per worker thread, since per-read scratch state can't be shared.

**Generation 1: a dense array sized by the reference, not the read.** One `Vec<BucketContent>` per
reference segment, sized up front from the segment's length divided by the algorithm's *minimum*
allowed half-length (`sz / MIN_HALFLEN + 2` slots) — on a multi-gigabase genome, a ~15 GB
allocation *per worker thread*. The reasoning behind this first design is straightforward: without
knowing in advance which buckets a read will touch, size for the worst case the algorithm permits.
The problem is that the worst case is enormously pessimistic — a single read only ever touches a
handful of buckets near where it maps, so the array sits almost entirely idle. Profiling found the
one-time allocation-plus-zero-init alone cost 7–21+ seconds per worker depending on contention —
this generation cost speed as well as memory, and in a genuinely counterintuitive way:
multithreaded whole-genome runs sometimes got *slower* with more threads, because a worker that
finishes this huge allocation last simply starts with zero reads left to process.

**Generation 2: size by what's touched, not by what's possible.** An `FxHashMap<BucketLoc,
BucketContent>` — only buckets a read actually reaches get an entry, so memory scales with reads
processed, not with reference size. This fixes the memory problem outright, but introduces a new
*speed* one: at k=15 on a whole genome, nearly every 15-mer window in a read has a match
*somewhere* in a 3+ Gbp reference, so a single read can touch millions of distinct buckets — and
every touch through this generation was a full hashmap `entry()` call: hash the key, probe for a
slot, possibly trigger a resize. Amortized O(1), but with a large constant next to "one indexed
write" — measured ~20% slower single-threaded than generation 1's array despite fixing its memory
blowup.

**Generation 2.5, still live today as the fallback for the cases below can't handle.** An
append-only `Vec` of raw contributions, merged once per read by an LSD radix sort on a packed
`(segm_id, b)` key rather than by a hashmap. This recovers the speed generation 2 lost — measured
at the time as 1.6x faster than the hashmap and, notably, already 25% faster than the C++ original
itself on whole-genome k=15 HiFi at `-@1` (1972.7 s vs 2637.2 s), byte-identical mapped/mapq
counts. What it still pays for is materializing every raw contribution before collapsing them: on
that same benchmark a read produced ~4M raw contributions collapsing to only ~242k *distinct*
buckets, and sorting 4M 32-byte records to do that collapse moved ~1.1 GB per read at
memory-bandwidth speed — 56% of total wall by itself. Its own internals were separately tuned
before generation 3 took over the common case, and they still carry the fallback traffic today:
the radix key's width is measured per read rather than fixed at 37 bits (cutting a pass, −30%),
the sorted record shrank 32→24 bytes by hoisting the per-read-uniform `i`/`seeds` fields out of
the per-entry payload, and every pass's histogram is built in one counting scan instead of one per
pass.

**Generation 3, current for the common case: a dense array again — but this time sized correctly.**
The insight that removes the sort entirely: the bucket space is small and *known*, once it's sized
by the *read's own* half-length rather than by the algorithm's coarsest-allowed one. A read with
half-length `l` partitions a reference of sketch length `n` into only `n / l` buckets total — for
a typical whole-genome HiFi read that is ~242k slots (the same figure generation 2.5 was
collapsing down to by sorting), each 16 bytes: **~4 MB, entirely L3-resident**, three to four
orders of magnitude smaller than generation 1's per-segment array because it scales with how
finely *this one read* divides the reference rather than with the smallest half-length the
algorithm ever allows anyone to request. Accumulation becomes one indexed read-modify-write —
`add_to_pos`/`add_to_bucket` no longer sort, deduplicate, or materialize a record at all. Capped
at `MAX_DENSE_SLOTS = 2 << 20` (2,097,152 slots × 16 bytes = exactly 32 MB per worker) as a safety
valve for the read that *doesn't* fit this profile — an unusually small half-length implies an
unusually large slot count — with generation 2.5 kept as the fallback beyond that cap, pinned by a
test asserting the two paths agree exactly. On a 6,000-read whole-genome HiFi run at `-@1`,
`bucket_merge` fell from 1342.1 s to 39.1 s and whole-run wall from 1995.6 s to 725.6 s,
byte-identical output.

One more deliberate choice inside generation 3, itself a small memory-vs-speed trade stated
explicitly: the array is *not* re-zeroed between reads. Extraction already clears every slot it
takes, so between reads the array is already all-empty — re-zeroing it anyway would cost O(slots)
per read regardless, measured at ~50 s of pure `memset` across a 240,000-read whole-genome run.
The one case that can leave it dirty — a read abandoned mid-accumulation, before extraction ran —
is handled precisely, by clearing only that read's own touched slots rather than the whole array
(`src/buckets.rs:196-238`, `:367`).

**Why this is not merely an implementation detail.** The paper specifies buckets abstractly
(Definition 10: a block covers a mapping if the mapping lies fully inside it) and says nothing
about how a block's accumulated state should be stored. All three generations, and specifically
the discoveries that drove each transition — the paradoxical more-threads-is-slower allocation
stall, the hashmap's per-touch cost on a dense-hit workload, the sort's memory-bandwidth cost —
are engineering entirely below the level the paper (or the C++, which shares its abstraction) ever
had to reason about.

### A.2 A segment's memory is freed the moment sketching no longer needs it

Multiple chunks of one reference segment share a single `Arc`'d copy of its raw bases (so parallel
sketching doesn't each hold a private copy). The `Arc` used to be carried all the way into the
completion message and released only once the — slower, serial — index-*apply* step consumed it;
narrowing its lifetime to release as soon as the segment is sketched, since nothing downstream
needs the raw bases again once its k-mers exist, cut **~11–13% off peak RSS** on indexing-dominated
runs. This is a lifetime decision, not an algorithmic one: the data was always safe to free at that
point, it simply hadn't been.

### A.3 The two-pass reference reader avoids reallocation churn as a side effect of being parallel

Covered for its speed contribution in §B.4 below, but it earns a mention here too: `PROFILING.md`
records that peak RSS falls slightly alongside the timing win, because pass 2 writes directly into
an exactly-sized buffer instead of the single-pass design's pattern of repeatedly reallocating a
growing per-segment buffer as more ranges arrived. A reallocating growth strategy transiently holds
both the old and new buffer during a copy; sizing the buffer exactly once removes that overhead
entirely rather than shrinking it.

### A.4 Sizing allocations from the actual distribution, not a flat safety margin

Sketch output buffers (`FracMinHash::sketch_slice_into`) are pre-reserved from a binomial model —
selection is a Bernoulli trial per k-mer, so the count of selected k-mers has mean `len · h_frac`
and standard deviation `√mean` — rather than a flat `1.1x` over-allocation. Reserving `mean + 6σ`
leaves the chance of a mid-sketch reallocation astronomically small while costing only ~0.5% slack
on a whole chromosome, against the ~10% a flat factor spends unconditionally on every sequence
regardless of its actual variance. Overflow stays merely slow (one extra reallocation), never
wrong.

A related, smaller case for the same reasoning: `h2multi` lists (k-mers with more than one
reference hit) start at capacity 2, not the default-grown capacity 1, since by construction a
k-mer that reaches this map already has at least two hits — sidestepping millions of
allocate-at-1-then-immediately-regrow calls across a whole-genome index, for identical final
memory.

### A.5 Why the `-@1` figure specifically is the honest one to quote

Every technique above lives inside per-worker state. At `-@1` there is exactly one `Buckets`
instance, so the 6.90–7.43x memory ratio is a clean single-instance comparison against the C++'s
one process. RESULTS.md §3c shows this ratio narrows at higher thread counts precisely because it
*is* per-worker: `N` threads hold `N` copies of a data structure that individually became tiny,
and tiny times many is no longer tiny. That is a property of parallelism multiplying a fixed
per-worker cost, not a regression in any of the memory work above — the per-worker cost genuinely
fell by three orders of magnitude; it just still multiplies.

---

## Part B — Single-thread speed: an accumulation, not one dominant change

Unlike the memory story, no single item here explains most of the 1.91–2.74x. Each is a real,
separately measured win; together they compound.

### B.1 Not recomputing the second-best search when the first already computed it

**Definition.** `match_rest` (§10 Scoring; the paper's Algorithm 1) finds a read's best mapping,
then searches again for the second-best — needed for mapq, Definition 6 — over the same surviving
buckets with a lower-bound cutoff. The paper calls `slideChain` once per search without discussing
whether they can share work.

**The discovery, precisely.** On the `Containment`/`Jaccard` path specifically, `find_best_mapping`
turns out to be a *pure* function of a bucket's location alone: it never reads the mutable
`content` that the pruning pass updates between the two searches, and it restores its own scratch
state (`diff_hist`) exactly as it found it — which is exactly what `best_fixed_length`'s closing
`debug_assert_eq!(intersection, 0)` exists to pin down. So the second sweep was recomputing
bit-identical scores for every bucket the first sweep had already scored. This is specifically
*not* true for `bucket_SH`/`bucket_LCS`: both build their result directly out of `content`, the
same mutable state pruning changes between sweeps — memoizing those would replay a stale value, not
a correct one, so the cache is restricted to the two metrics where it's provably safe
(`src/shmap/scoring.rs:401`, and the comment immediately above it).

**Implementation.** `RefineCache` records the first sweep's results and replays them in the
second; because both sweeps walk the identical `sorted_buckets` slice in the identical order,
replay is a monotone cursor over one reusable `Vec` — no hashing, no lookup structure at all.

**Not a full hit by construction.** The first pass's acceptance threshold ratchets upward as its
own best score improves, so a bucket the first pass discards late can still clear the second
pass's flatter cutoff. Those are genuine misses, recomputed from scratch — which is exactly why the
measured win below is 44%, not 100%.

**Measured.** 44% of `find_best_mapping` calls eliminated, flat across coverage (1x/3x/10x) on real
HG002 HiFi against the whole genome at `-@1`: `match_rest_for_best2` −66.4%/−67.1%/−66.5%, `refine`
−39.2%/−41.3%/−39.8%, `query_mapping` −9.6%/−10.5%/−9.4%. PAF output byte-identical;
`SHMAP_NO_REFINE_MEMO=1` disables it for a direct A/B on one binary.

### B.2 The bucket sort: the paper prescribes it, the C++ implements it plainly, shmap-rs implements it faster and deterministically

**In the paper, precisely.** This is not an invented optimization — the paper's own Algorithm 4
names it as "Optimization 2: Sort block by decreasing number of matches," with the stated
reasoning that the *previous* optimization (raising the acceptance threshold as better mappings
are found, to reject more blocks sooner) works best when the threshold rises as early as possible,
and sorting by descending match count visits the most promising blocks first — "the heuristic
expectation that the blocks with most matches will have a higher similarity, which will increase
θ earlier."

**In the C++.** `get_sorted_buckets` (§8) implements exactly this with `std::sort` on the full
bucket records — not a stable sort, so buckets tied on match count get whatever relative order the
implementation happens to produce, which the standard doesn't even guarantee between runs of the
same binary.

**In shmap-rs.** Sorts a *packed* 64-bit key instead of the bucket records themselves —
`((u32::MAX - matches) as u64) << 32 | index`, ascending, with `sort_unstable`
(`src/buckets.rs:657-682`). Packing each entry's original index into the low 32 bits makes this
reproduce exactly what a *stable* descending-by-`matches` sort would: ties keep their original,
location-sorted order, purely as a side effect of the key, with no stability bookkeeping in the
sort itself.

**Computation.** Sorts 8-byte keys instead of the ~32-byte `(BucketLoc, BucketContent)` records
directly — on a k=15 whole-genome read (~240k touched buckets) this moves a quarter of the bytes a
record-sort would move, and needs no stable-sort temporary allocation, while implementing the same
paper-prescribed ordering.

**A useful side effect, not the motivation.** Deterministic tie-breaking regardless of scheduling
or standard-library version is why B01's records agree with the C++ on all 12 core PAF columns
98.32% of the time under Containment (RESULTS.md §7), and the disagreements are almost entirely
coordinate-only on the same target — a property of the C++ not guaranteeing tie order, not a port
regression.

### B.3 The rolling-hash inner loop: precomputed rotations, and no bounds checks

**Definition.** `FracMinHash::sketch_slice_into` (§5) computes a forward and reverse-complement
ntHash-family rolling hash per k-mer window, each window derived from the previous by rotating and
XORing in the incoming/outgoing base's contribution — the single hottest loop in the whole mapper,
run once per base of the reference and once per base of every read.

**Precomputed rotation tables.** Three additional 256-entry lookup tables are precomputed once per
`FracMinHash` instance, each holding a base's contribution with a *fixed* rotate already folded in:
`lut_fw_k[c] = lut_fw[c].rotate_left(k)`, `lut_rc_r1[c] = lut_rc[c].rotate_right(1)`, `lut_rc_k1[c]
= lut_rc[c].rotate_left(k-1)` (`src/sketch.rs:37-52`). The mechanical reason this works: those
three rotate *amounts* — `k`, `1`, `k-1` — depend only on the window geometry, never on which base
is being processed, so they can be composed into the table once instead of applied at every use.
The rolling update loop then does a plain table load per base instead of a load-then-rotate,
removing 3 of the loop's 5 per-base rotates across the whole reference and every read. Tried and
rejected: interleaving each base's forward/reverse pair into one `[Hash; 2]` table to halve the
load count, measured ~6% *slower* instead — the 16-byte load goes through a vector register and has
to be split apart again before the scalar XORs, costing more than the extra scalar load it was
meant to remove.

**Bounds-check-free iteration.** The update used to index the sequence at `s[r]` and `s[r - k]`
through a signed `RPos`, which cost two bounds checks plus a sign-extension per base — and a
mid-loop `r >= s.len()` break stopped LLVM from recognizing the loop as counted at all, losing
further optimization opportunities. Walking the incoming and outgoing bases as a pair of zipped
slice iterators lets the compiler prove the indices are always in range from the iterators'
lengths alone, removing every check: **~13–17% off `index_sketching`**.

**Computation.** Both are constant-factor work removed per base, not a complexity change — the
loop is still O(sequence length) either way; it simply does less work per iteration of that same
loop.

### B.4 The reference reader: parallel, in two passes, specifically to keep the *copy* parallel too

**Where the cost was.** Reading isn't I/O-bound — the 3.18 GB human reference streams off page
cache at 3.7 GB/s, 0.87 s total — so the real cost is line-splitting, newline-stripping, and
copying bases into segment buffers.

**Why one pass isn't enough.** A first parallel design split the file into 16 MB byte ranges parsed
by up to 8 workers in a single pass, but instrumenting it found the win was capped by the
*collector*: workers spent only 0.05 s waiting to hand off results, while 2.8–3.2 s of a 2.9–3.2 s
total read went into concatenating those ranges into a growing per-segment buffer on one thread —
not the memory copy itself, but the doubling reallocations and ~780k serialized first-touch page
faults that come with growing a buffer incrementally.

**The fix.** Two passes instead: pass 1 only *counts*, walking the same byte ranges in parallel to
determine every segment's exact final size and every range's exact offset within it; pass 2 then
lets worker threads write straight into disjoint slices of a buffer that is already the right size
— no reallocation, no growth, first-touch page faults spread across threads instead of serialized
on one. Both passes drive the same line-walking logic so they cannot disagree about where a line
boundary falls, and two `debug_assert`s pin that pass 2 writes exactly what pass 1 counted and that
a segment's parts tile its buffer with no gaps.

**Measured.** 4.4 s → 1.5–1.7 s (`fasta_scan` ~0.3 s + `fasta_fill` ~1.3 s). Falls back to the
original single-pass reader for compressed input (byte offsets are meaningless there), small
files, `-@1`, and non-Unix targets — behavior is unchanged wherever the split doesn't apply.

### B.5 Global allocator swap (community contribution, PR #5)

`mimalloc` replaces the system allocator globally (`src/main.rs:13`) — a two-line change,
contributed by a community PR rather than part of the original port, and orthogonal to everything
above: an allocator choice cannot change algorithm results, confirmed by re-running all nine
ground-truth accuracy figures bit-for-bit identical after the swap. Measured effect (promotion
commit `bacfec3`): single-threaded speedup over the C++ moved from 1.85–2.55x to 1.91–2.74x — the
range quoted at the top of this document already reflects it. At high sketch density (`-r 0.10`)
the effect is larger: wall time fell 35% (64.9 s → 42.4 s) and peak memory 4% (17.74 GB → 17.00
GB). The one place it costs something: baseline single-threaded memory at the default sampling
rate rose slightly, 2.21 GB → 2.73 GB — the allocator trades a small fixed overhead for
substantially better behavior under the allocation pressure the denser settings create.

### B.6 Smaller wins, catalogued in full in `PROFILING.md`

Below the threshold of their own section, but real and measured, each below the level of
abstraction the paper or the C++ ever specifies — full detail and exact figures in
`PROFILING.md`'s "What's optimized" list rather than repeated here:

- **Chunked sketching of a reference segment**, not one whole segment per worker — removes the
  Amdahl cap where indexing couldn't finish faster than sketching the single longest chromosome
  (~18% off `indexing` at `-@8`; relevant to single-thread speed because the same chunking logic,
  not just its parallelism, is what `sketch_slice_into`'s offset-aware slicing enables).
- **FASTA records handed to the caller by value**, removing a second full copy of every
  chromosome that both call sites used to make with `.to_vec()`, since the underlying reader
  already returns an owned buffer.
- **`match_seeds`'s per-seed scratch is two fixed slots, not a `Vec`** — a hit at bucket `b`
  touches only `b` and `b - 1`, and `b` never decreases within a segment, so the live window is
  provably two buckets wide; no dynamic growth is ever possible.
- **Buffered stdout in the collector** — one `BufWriter` held for the whole run instead of
  `print!()` per read, which flushes (a syscall) on every trailing newline.
- **`lto = "fat"` + `codegen-units = 1`** in the release profile — worth ~5% wall on its own,
  purely from cross-crate inlining across the `needletail`/`rustc-hash` boundary that the default
  16 codegen units prevented. `panic = "unwind"` stays as-is (not `"abort"`) because the per-read
  panic isolation the multithreaded pipeline relies on needs `catch_unwind` — a case where a
  build-profile choice is constrained by a capability outside this document's scope, not by
  anything about speed itself.

---

## Not covered here, and where it lives instead

- **Correctness fixes** — five places where shmap-rs's output differs from what the C++ or the
  paper would produce (an uncleared-counters bug that corrupts two live PAF tags across a run,
  a Jaccard scoring off-by-one the C++'s own author flagged as unresolved, a bucket-coordinate bug
  that produced a mapping 1.28 Mb past a chromosome's end, and two cases of undefined C++ behavior
  that had no faithful Rust translation). These change *what* gets computed, not its cost, so they
  don't belong in a document about speed and memory.
- **The multithreading capability** — the reader/worker-pool/collector pipeline and parallel
  sharded indexing are new engineering absent from the C++ entirely, and account for RESULTS.md's
  largest headline numbers (up to 17.79x whole-run speedup) — but they are explicitly not a
  single-thread story: `-@1` runs through the identical pipeline and pays none of its overhead
  while gaining none of its benefit. Index sharding's *storage* choices (a fixed-size shard array
  rather than a `Vec`, to avoid a colder indirection in the mapping-time lookup path) are the one
  piece of that work with single-thread consequences, since every index probe pays that cost
  regardless of `-@`.
- **New CLI-visible capabilities** — `--rarity-weight`/`--rarity-tiebreak`/`--rarity-alpha`
  (research knobs for repeat-region accuracy, ultimately not the fix for that gap — RESULTS.md §8),
  `-x`/`--profile` and `--per-read-stats` (profiling instrumentation), all off by default and none
  changing the cost of a default run.
- **Behavior kept deliberately unchanged from the C++** — two counters ported as the same inert,
  self-consistent bumps the C++ has, because they feed only a diagnostic never a PAF tag; two dead
  CLI flags kept for compatibility; one case where the C++'s own code comment describes a formula
  the code beneath it doesn't actually implement.
