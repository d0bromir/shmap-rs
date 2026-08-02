# What changed: shmap-rs vs the C++ `shmap` and the map-shmap paper

Every entry below answers four questions: what the concept is, what the paper or the C++
reference implementation does, what shmap-rs does differently, and why. Ordered by
significance — correctness first, then the architecture that gives the port its speed, then
data-structure and algorithmic optimizations within the ported logic, then new opt-in
capabilities, then quirks kept on purpose.

**Scope.** This is about the *port*: where shmap-rs's behavior, data structures, or capabilities
differ from the paper (Ivanov & Medvedev, *map-shmap: Practical long-read mapping with seed
heuristic on sketches*) or from the C++ reference binary. It is not a benchmark report — see
[RESULTS.md](RESULTS.md) for measured speed/memory/accuracy — not a process document — see
[CONTRIBUTING.md](CONTRIBUTING.md) — and not the engineering log — see
[PROFILING.md](PROFILING.md), which records *every* optimization chronologically with the exact
before/after numbers measured at the time it landed, deliberately never updated. This document is
organized differently on purpose: by significance rather than by time, framed against the paper
and the C++ specifically rather than against the previous build, and covering correctness fixes
and new capabilities that a performance log has no reason to carry. Tier 3 below cites
PROFILING.md directly rather than re-deriving its numbers. For the base algorithm's own
definitions (sketch, bucket, seed heuristic, mapq, …), see the reverse-engineered reference at
[`docs/shmap_algorithm.pdf`](docs/shmap_algorithm.pdf) (chapters cited below as, e.g., §5
Sketching); this document only restates a definition where the *change itself* needs one.

**Methodology and its limit.** The C++ comparisons below come from the Rust source's own doc
comments, written against the C++ source at port time and confirmed by grep-based call-site
audits (a documented practice — see `docs/sections/16_defects.tex`'s own methodology note). The
C++ *source* is not available on this host — only its release binary (`/home/mpiuser/Pesho/shmap/
release/shmap`) — so where a comment doesn't make a C++ claim, this document doesn't invent one.
Several of the bugs below were found by validating *output invariants* (structural/score
consistency, ground-truth position checks — see RESULTS.md §7) rather than by diffing against
the C++ build, which is the only method that can catch a defect both builds share.

---

## Tier 1 — Correctness: changes to what gets computed

These change actual output — mapped coordinates, scores, or the diagnostic fields in the PAF —
relative to what the C++ produces or the paper specifies. They rank first because everything
downstream (RESULTS.md's accuracy claims, the `wrong_q60` gate) assumes these are right.

### 1.1 The C++ never clears its per-read counters — corrupting live PAF tags, not just diagnostics

**Definition.** Each read's mapping produces a set of run-scoped counters (matches examined,
buckets touched, …), some of which are written directly into the output PAF as tags —
`total_matches:i:` and `match_inefficiency:f:` (§13 Output).

**In the C++.** `map_read`'s per-read `Counters C` is never actually reset between reads:
`C.clear()` is commented out in the source, and the `C.init(...)` call that follows only
registers a partial list of the names the function goes on to increment. Left as written, every
counter not in that partial list — `total_matches` among them — keeps accumulating across every
read a single run processes, and gets merged into the run-wide totals a second time via `H->C +=
C` after each read, compounding further.

**In shmap-rs.** `map_read` fully clears and re-registers the per-read counter set at the top of
every call (`src/shmap/mod.rs:634-635`, against the corrected, complete counter list documented at
`src/shmap/mod.rs:93` — deliberately kept as *a superset* of the C++'s partial one).

**Computation.** No asymptotic change — the reset is O(number of counters), already paid every
read. The difference is *value*, not cost: in the C++, `total_matches:i:` and
`match_inefficiency:f:` for the *N*-th read reflect accumulated state from reads 1..N, not read N
alone.

**Why.** This is squarely inside the port's stated "fix real bugs" boundary (`src/shmap/
mod.rs:15`): unlike the two inert counters kept faithfully wrong (§5.1 below), this one feeds a
value a caller reads directly off the PAF line, not a diagnostic nobody parses. Left unfixed, a
per-read live tag would silently describe an accumulating quantity instead of a per-read one.

### 1.2 Jaccard's sliding-window scoring used the wrong loop variable

**Definition.** `best_included_jaccard` (§10 Scoring) scores each candidate window with a
two-pointer sweep over sorted matches, growing/shrinking `[l, r)` and re-scoring at the point of
maximum Jaccard similarity.

**In the C++.** The inner loop scores using `matches[prev(r)]` — one element *before* the current
`r` — on the same line that increments `intersection` for the element *at* `r`. The original
author left `// TODO: should it be prev(r) instead?` on that exact line, confirming this was
unresolved, not deliberate. The result: `intersection` reflects one element more than the span
being scored, which can produce `intersection > s_sz` — violating an invariant the scoring
function's own `debug_assert` checks, and which no C++ test exercises.

**In shmap-rs.** Scores against `r` (`src/refine.rs:133`), consistent with what `intersection`
already counts at that point in the loop.

**Computation.** No complexity change — same two-pointer sweep, same O(n) pass. The set of
candidate window boundaries considered shifts by one match per iteration where it matters,
changing which window (and therefore which score) the Jaccard metric reports as best for a read.

**Why.** No oracle to defer to (no C++ test covers this code path), so the port takes the
internally-consistent reading: score what `intersection` actually counted.

### 1.3 A single-hit bucket match could pull in a k-mer from the wrong reference segment

**Definition.** `matches_in_bucket` (§9 Pruning) extends a candidate bucket with one seed's
matches; a k-mer with exactly one genome-wide hit takes a fast single-hit path instead of the
general multi-hit range search.

**In the C++.** The single-hit branch tests only whether the hit's position falls in this
bucket's span — never whether it's in the *same segment* (`segm_id`). The multi-hit branch checks
`segm_id` in both its `lower_bound` and its scan loop; the single-hit branch, doing less work, was
missing the check the multi-hit branch has twice. A k-mer whose one hit lies in a *different*
chromosome, at a coordinate that numerically falls inside this bucket's span, was counted in:
inflating `matches` (weakening pruning everywhere) and merging a wrong-segment coordinate into
`r_min`/`r_max`.

**In shmap-rs.** The single-hit branch checks `hit.segm_id == b.segm_id` alongside the position
test (`src/shmap/pruning.rs:24`).

**Computation.** No complexity change — one extra integer comparison per single-hit match.

**Why.** Invisible under `Containment`/`Jaccard`, which recompute coordinates in
`best_fixed_length` and discard `r_min`/`r_max` — but `bucket_SH` reports those coordinates
directly, and on real HG002 HiFi data this produced a reported span 1.28 Mb past the end of
chromosome 6 (RESULTS.md §7). Found by validating output invariants (a mapping cannot legally end
past its target's length), not by diffing — the C++ shares the exact same bug, so a build-vs-build
diff cannot see it.

### 1.4 Unmapped reads: C++ undefined behavior has no faithful Rust translation

**Definition.** `map_read` returns an optional best mapping; when a read doesn't clear the
similarity threshold, there is none.

**In the C++.** `map_read` unconditionally calls `best->set_global_stats(...)` /
`best->print_paf(...)` even when `best` is `std::nullopt` — dereferencing an empty
`std::optional`, undefined behavior in C++.

**In shmap-rs.** There is no Rust translation of "dereference `None`" that isn't a guaranteed
panic, so this needed a real design decision rather than a port: unmapped reads get a minimal
record (query id, read length, `*` fields) written to `.unmapped.paf` instead
(`src/shmap/mod.rs:20`).

**Why.** Not optional — `Option<T>::unwrap()` on `None` is defined behavior (panic) in Rust, so
reproducing the C++'s UB was never on the table. The design choice was *what* to do instead, made
once when the port was planned.

### 1.5 The sketch hash table treats unknown bases as UB in C++, zero deterministically in Rust

**Definition.** `FracMinHash`'s rolling hash (§5 Sketching) looks up each base's contribution in a
256-entry table indexed by the raw ASCII byte.

**In the C++.** `hash_t LUT_fw[256]` is a raw, uninitialized stack array; `initialize_LUT()` fills
exactly 8 of its 256 slots (upper/lowercase A/C/G/T). Reading any other byte — `N`, an ambiguity
code, anything non-ACGT — reads uninitialized stack memory: undefined behavior.

**In shmap-rs.** The table is zero-initialized before the 8 real entries are filled
(`src/sketch.rs:73`), so an unknown base deterministically contributes a hash of `0`.

**Why.** Well-defined behavior for the same set of inputs (any ACGT-only sequence) that the C++
handles correctly, and a defined answer instead of UB for everything else — without changing
output for any input the C++ itself doesn't crash or vary on.

---

## Tier 2 — Architecture: capabilities absent from the C++ entirely

The paper describes a single-pass, single-threaded algorithm; nothing in it specifies
parallelism. These are new engineering, not reinterpretations of anything in the paper, and they
account for most of RESULTS.md's headline numbers (up to 17.79x whole-run speedup, indexing cut
from ~9.4 s to ~3 s).

### 2.1 A full multithreaded mapping pipeline, deterministic regardless of thread count

**In the C++.** No threading anywhere — confirmed by grep, zero matches
(`src/shmap/mod.rs:37`).

**In shmap-rs.** `map_reads` runs a fixed three-stage pipeline over `std::thread::scope`: one
reader thread streams records off disk and dispatches them as `Job`s over a bounded channel;
`-@` worker threads each own an independent `SHMapper` + `Buckets` (per-read scratch cannot be
shared) and turn each job into a `ReadOutput`, tagged with its original index; the scope's own
thread is the sole collector, reordering completions by index and applying them
(`apply_read_output`) strictly in input order.

**Data structure.** The bounded channel caps memory to a few jobs ahead of the workers (not the
whole file); the collector's reorder buffer is a `HashMap<u64, Done>` holding only completions
that arrived out of order, never more than the number of in-flight jobs.

**Computation.** Turns the read-mapping phase from O(reads) wall-clock-serial into
O(reads / threads) for the CPU-bound part, with the collector's own O(reads) reordering pass
staying serial but cheap (no per-read compute, just a hashmap insert/remove).

**Why determinism, specifically.** Output — stdout, the PAF, `.unmapped.paf`, `paul.tsv` — is
byte-identical at every thread count because the collector applies strictly in submission order;
only the CPU-bound mapping work parallelizes. This is verified by a dedicated test
(`tests/multithreaded_parity.rs`) and is what makes RESULTS.md's thread-scaling table meaningful
at all — a mapper that produced different output at different thread counts couldn't be
benchmarked this way.

**Robustness, layered on top.** Each worker catches panics from its own `map_read` call
(`catch_read_panic`) rather than letting one bad read kill the thread — without this, a dead
worker stops draining the bounded channel, the reader blocks trying to send into it, and the main
thread blocks forever in `reader.join()`, turning one bad read into a permanent hang. Found by
reproducing exactly this hang (`-v 2` against reads without ground-truth-encoded headers, which
panics by documented design).

### 2.2 Parallel, sharded reference indexing

**In the C++.** Indexing is entirely single-threaded (`src/index.rs:7`).

**In shmap-rs.** Two techniques combine (numbers below from `PROFILING.md`, which tracks them
against the previous build rather than the C++):

- **Hash-sharded index storage: 8.4 s → 1.1–1.8 s.** The k-mer hash table is split into
  `N_SHARDS = 8` independent shards (`src/index.rs:73-74`), each its own `HashMap`. Every
  occurrence of a given hash lands in the same shard by construction (`shard_of`, a pure function
  of the hash), so the parallel fill needs no locking and cannot depend on scheduling. Fixed at 8
  regardless of `-@`, so the index's *contents* never depend on thread count. Three design
  choices had to be right, each found by measuring a wrong first attempt:
  - *Shard on the low hash bits, against the usual advice.* FracMinHash keeps only hashes below
    `h_frac · u64::MAX`, so every hash reaching the index is small — at `-r 0.01`, under `2^57`,
    with the top 7 bits always zero. Sharding on the high bits (normally the best-mixed choice)
    puts every k-mer in shard 0 and silently serializes the whole build: an early version did
    exactly this with 64 shards, and it cost 6.96 s on one thread while the other 63 finished in
    0.13 s, with output still correct (`src/index.rs:76-84`).
  - *Keep the fill interleaved with reading at `-@1`.* Deferring inserts to a separate phase loses
    the overlap the collector already had with the reader and sketcher: 9.2 s → 15.1 s, slower.
  - *Hold the shards in a fixed-size array, not a `Vec`.* Every index probe in the mapping hot
    path goes through this array; the extra dependent load through a `Vec`'s indirection cost
    `collect_kmer_info` up to 24%.
- **A two-pass parallel FASTA reader: 4.4 s → 1.5–1.7 s**, used for the reference only
  (`src/io/mod.rs:172`; `fasta_scan` ~0.3 s + `fasta_fill` ~1.3 s). Pass 1 counts each segment's
  exact size and every byte range's offset within it, in parallel; pass 2 lets worker threads
  write straight into disjoint slices of a pre-sized buffer, with zero reallocation. A first
  parallel attempt (16 MB ranges, up to 8 workers, single pass) was measured to spend 2.8–3.2 s of
  a 2.9–3.2 s read on concatenating ranges into a growing per-segment buffer on one thread — not
  the copy itself, but the doubling reallocations and ~780k serialized first-touch page faults.

**Computation.** Turns indexing from a serial O(reference length) pass into an
O(reference length / threads) parallel one for both reading and sketching, with a small
fixed-shard-count insert phase that doesn't grow with `-@`.

**Why 8 shards, not more.** More shards cost real money elsewhere: every index probe in the
*mapping* hot path indexes this array, and a wider one is colder. Measured on real HiFi at 10x
`-@1`, where mapping is ~98% of the wall, 64 shards cost 3.4% overall while 8 cost 0.7%
(`src/index.rs:65-72`).

---

## Tier 3 — Data structures and algorithms within the ported logic

These are optimizations to *how* the paper's own algorithm is computed — same asymptotic
behavior the paper argues for, different concrete implementation — found and refined across
several internal iterations, not single decisions made once.

### 3.1 The bucket accumulator: three generations to reach its current form

**Definition.** `Buckets` (§8 Bucketing) accumulates per-bucket match counts and coordinate
ranges while a read is scored against candidate reference windows.

**Generation 1.** One dense `Vec<BucketContent>` per reference segment, sized up front from the
segment's length (`sz / MIN_HALFLEN + 2` slots) — on a multi-gigabase genome, a ~15 GB allocation
*per worker thread*. Profiling found this one-time allocation-plus-zero-init cost 7–21+ seconds
per worker depending on contention, and was the direct cause of multithreaded whole-genome runs
sometimes getting *slower* with more threads: a worker that finishes this allocation last starts
with zero reads left.

**Generation 2.** An `FxHashMap<BucketLoc, BucketContent>` — only touched buckets exist, so memory
scales with reads processed, not reference size. This fixed the memory blowup but introduced a
*speed* regression on repetitive references: k=15 seeds on a whole genome touch millions of
buckets per read, and every touch was a full hashmap `entry()` (hash, probe, possible resize) —
~20% slower single-threaded than the original dense array despite the memory win.

**Generation 2.5, still live as the fallback below.** An append-only `Vec` of raw contributions,
merged once per read by an LSD radix sort on a packed `(segm_id, b)` key — the pass count is
computed per read (skipping always-zero high bits) rather than fixed, since `b` is usually far
smaller than its 32-bit budget. Recovered the speed lost in generation 2 and then some: measured
at the time as **1.6x faster than the generation-2 hashmap, and 25% faster than the C++ original**
on whole-genome k=15 HiFi at `-@1` (1972.7 s vs 2637.2 s), byte-identical mapped/mapq counts
(`PROFILING.md`). The remaining cost was materializing every raw contribution: on that benchmark a
read produced ~4M raw contributions collapsing to only ~242k distinct buckets, and sorting 4M
32-byte records to collapse them moved ~1.1 GB per read at memory-bandwidth speed — 56% of total
wall by itself.

**Generation 3, current for the common case.** A dense array again — but sized from the *read's
own* half-length divided into the reference, not the reference length over the algorithm's
minimum half-length: three to four orders of magnitude smaller than generation 1's array, since it
scales with how finely *this read* partitions the reference rather than with the coarsest
partition the algorithm ever allows. Capped at `MAX_DENSE_SLOTS` (2 << 20 slots) with generation
2.5 kept as the fallback beyond that cap (`src/buckets.rs:196-238`), pinned by a test asserting
the two paths agree exactly. Accumulation becomes one indexed read-modify-write — no sort, no
dedup pass, no per-contribution record: `bucket_merge` fell from 1342.1 s to 39.1 s and whole-run
wall from 1995.6 s to 725.6 s on a 6,000-read whole-genome HiFi run at `-@1`, byte-identical
output (`PROFILING.md`). The array is deliberately *not* re-zeroed between reads (extraction
already clears every slot it takes); the one case that can leave it dirty — a read abandoned
mid-accumulation — is handled by clearing only that read's touched slots (`src/buckets.rs:367`,
~50 s of avoided `memset` on a 240k-read whole-genome run).

Generation 2.5's own internals were separately tuned before generation 3 existed to replace it for
most reads — worth naming since they still carry the short-read/huge-halflen fallback traffic
today: the radix key's width is measured per read rather than fixed at 37 bits (cutting a pass,
−30%), the sorted record shrank 32→24 bytes by hoisting the per-read-uniform `i`/`seeds` fields
out of it, and all passes' histograms are built in one counting scan rather than one per pass.

**Why this belongs in "changes vs the paper."** The paper specifies buckets abstractly
(Definition 10, a mapping covered by a block) and says nothing about their storage; all three
generations, and the discoveries that drove each transition (the paradoxical more-threads-is-
slower bug, the hashmap regression, the sort cost), are engineering entirely below the paper's
level of abstraction.

### 3.2 Memoizing the second-best search against the best-mapping search

**Definition.** `match_rest` (§10 Scoring, and the paper's Algorithm 1) finds a read's best mapping,
then searches again for the second-best — the one needed to compute mapq (Definition 6) — over the
same set of surviving buckets with a lower-bound cutoff. The paper's Algorithm 1 calls
`slideChain` once per search without discussing whether the two searches share work.

**In shmap-rs.** On the `Containment`/`Jaccard` path, `find_best_mapping` is a pure function of a
bucket's location: it never reads the mutable `content` that pruning updates between the two
searches, and it restores `diff_hist` (the sliding-window scratch state) exactly as it found it —
which is what `best_fixed_length`'s closing `debug_assert_eq!(intersection, 0)` pins. So the second
sweep was recomputing bit-identical scores for every bucket the first sweep already scored.
`RefineCache` records the first sweep's results and replays them in the second; both walk the same
slice in the same order, so replay is a monotone cursor over one reusable `Vec`, with no hashing.
Not a full cache hit by construction: the first pass's threshold ratchets upward as its own best
score improves, so a bucket it prunes late can still clear the second pass's flatter cutoff — those
misses are recomputed from scratch, which is why the win below is 44%, not 100%.

**Computation.** Eliminates 44% of `find_best_mapping` calls, measured flat across coverage
(1x/3x/10x) on real HG002 HiFi against the whole genome at `-@1`: `match_rest_for_best2`
−66.4%/−67.1%/−66.5%, `refine` −39.2%/−41.3%/−39.8%, `query_mapping` −9.6%/−10.5%/−9.4%
(`PROFILING.md`). PAF output is byte-identical; `SHMAP_NO_REFINE_MEMO=1` disables it for A/B
comparison against one binary.

**Why it belongs here.** Not a change to *what* gets computed (§1's bar) or a new capability
(Tier 2) — the second search still runs, over the same buckets, with the same cutoff. It is a
change to *how many times* the underlying scoring function actually executes, entirely below
where the paper's Algorithm 1 or the C++'s implementation of it draws any distinction.

### 3.3 Reproducing a stable sort with an unstable one, on packed keys

**In the C++.** `get_sorted_buckets` (§8) sorts touched buckets by descending match count with
`std::sort` — not stable, so buckets tied on match count get whatever relative order the
implementation happens to produce, which the standard doesn't guarantee even between runs of the
same binary.

**In shmap-rs.** Sorts a *packed* representation instead of the bucket records themselves:
`((u32::MAX - matches) as u64) << 32 | index`, ascending, with `sort_unstable`
(`src/buckets.rs:657-682`). This reproduces exactly what a *stable* descending-by-`matches` sort
would produce — ties keep their original, location-sorted order — because the low 32 bits of the
key are each entry's original index.

**Data structure.** Sorts 8-byte keys instead of the ~32-byte `(BucketLoc, BucketContent)`
records directly: on a k=15 whole-genome read (~240k touched buckets) this moves a quarter of the
bytes a record-sort would, and needs no stable-sort temporary allocation.

**Why deliberately stable, unlike the C++.** Deterministic tie-breaking regardless of scheduling
or standard-library version. This is the documented explanation for why B01's records agree with
the C++ on all 12 core PAF columns 98.32% of the time under Containment (RESULTS.md §7,
`impl_agreement`) and the disagreements are almost entirely coordinate-only on the same target
(2,482 of 2,513, RESULTS.md §2) — a property of the C++ reference not guaranteeing tie order
among equally-scored buckets, not a port regression.

### 3.4 The rolling-hash inner loop: precomputed rotation tables, and no bounds checks

**Definition.** `FracMinHash::sketch_slice_into` (§5) computes a forward and reverse-complement
ntHash-family rolling hash per k-mer window, each window derived from the previous by rotating and
XORing in the incoming/outgoing base's contribution.

**Precomputed rotation tables.** Three additional 256-entry tables are precomputed once per
`FracMinHash` instance, each holding a base's contribution with a *fixed* rotate already applied:
`lut_fw_k[c] = lut_fw[c].rotate_left(k)`, `lut_rc_r1[c] = lut_rc[c].rotate_right(1)`, `lut_rc_k1[c]
= lut_rc[c].rotate_left(k-1)` (`src/sketch.rs:37-52`). The rolling update loop then does a plain
table load per base instead of a load-plus-rotate, since the rotate amounts (`k`, `1`, `k-1`) are
the same on every iteration — removing 3 of the loop's 5 per-base rotates. Tried and rejected:
interleaving each base's forward/reverse pair into one `[Hash; 2]` table, to halve the load count,
measured ~6% *slower* — the 16-byte load goes through a vector register and has to be split apart
again before the scalar XORs, costing more than the extra scalar load it removes.

**Bounds-check-free iteration.** The update indexed the sequence at `s[r]` and `s[r - k]` by a
signed `RPos`, paying two bounds checks plus sign-extension per base, and a mid-loop `r >=
s.len()` break stopped LLVM from treating the loop as counted at all. Walking the incoming and
outgoing bases as a pair of zipped slice iterators removes all of it: **~13–17% off
`index_sketching`** (`PROFILING.md`).

**Computation.** Both are per-base constant-factor work eliminated, not an algorithmic complexity
change — still O(sequence length) either way.

### 3.5 Global allocator swap (community contribution, PR #5)

**In shmap-rs.** `mimalloc` replaces the system allocator globally (`src/main.rs:13`,
contributed by a community PR, not part of the original port). Output is unaffected — an
allocator choice cannot change algorithm results, confirmed by re-running all nine ground-truth
figures bit-for-bit identical after the swap.

**Measured effect** (from the promotion commit, `bacfec3`): single-threaded speedup over the C++
moved from 1.85–2.55x to 1.91–2.74x; at high sketch density (`-r 0.10`) wall time fell 35% (64.9 s
→ 42.4 s) and peak memory 4% (17.74 GB → 17.00 GB). Single-threaded baseline memory rose slightly
(2.21 GB → 2.73 GB) — the allocator trades a small fixed overhead for better behavior under
allocation pressure.

### 3.6 Smaller wins, catalogued in full in `PROFILING.md`

Below the threshold of their own entry here, but real, measured, and each below the paper's level
of abstraction — full detail and numbers in `PROFILING.md`'s "What's optimized" list rather than
repeated here:

- **Chunked sketching of a reference segment**, not one segment per worker — removes the Amdahl
  cap where indexing couldn't finish faster than sketching the single longest chromosome (~18% off
  `indexing` at `-@8`).
- **FASTA records handed to the caller by value** instead of copied a second time with `.to_vec()`
  at both call sites, since the underlying reader already returns an owned buffer.
- **A segment's read-ahead is bounded to its own chunks**, not `threads * 4` chunks, so its memory
  frees as soon as it's sketched rather than staying alive until the (slower) index-apply step —
  ~11–13% off peak RSS on indexing-dominated runs.
- **Sketch buffer capacity from the binomial distribution** (mean + 6σ) instead of a flat `1.1x`
  factor — ~0.5% slack on a chromosome instead of 10%, with overflow merely slow, never wrong.
- **`h2multi` lists start at capacity 2**, not 1, since a k-mer that reaches the multi-hit map
  always has at least two hits by construction — avoiding millions of single-element-then-grow
  reallocations across a genome.
- **`match_seeds`'s per-seed scratch is two fixed slots, not a `Vec`** — a hit at bucket `b`
  touches only `b` and `b - 1`, and `b` never decreases within a segment, so the live window is
  provably two buckets wide.
- **Buffered stdout in the collector**, one `BufWriter` held for the whole run instead of
  `print!()` per read.
- **`lto = "fat"` + `codegen-units = 1`** in the release profile, worth ~5% wall on its own —
  cross-crate inlining across the `needletail`/`rustc-hash` boundary that 16 default codegen units
  prevented. `panic = "unwind"` stays as-is rather than moving to `"abort"`: the per-read panic
  isolation in §2.1 depends on `catch_unwind`.

---

## Tier 4 — New opt-in capabilities

CLI-visible additions with no effect on default behavior — every one is either off by default or
gated behind a flag absent from the C++ entirely.

### 4.1 `--rarity-weight`, `--rarity-tiebreak`, `--rarity-alpha` — research knobs for repeat accuracy

Not present in the C++ at all. Weight a matched k-mer by `1 / hits_in_reference^alpha` instead of
counting every match equally (`--rarity-weight`), or use rarity only to break near-ties between
candidate buckets (`--rarity-tiebreak`, `--rarity-alpha`) — both aimed at the satellite/rDNA
accuracy gap analyzed in RESULTS.md §8. `--rarity-weight 0` (the default) is byte-identical to the
C++ path. **Outcome, for completeness:** both were measured and rejected as the fix for that gap —
raising sketch density (`-r`) closes it instead; the knobs are kept as research instrumentation,
not a recommendation (RESULTS.md §8).

### 4.2 `-x`/`--profile` + `--profile-log` — per-stage profiling

Not present upstream. Emits a JSON report of per-stage timers and counters (indexing sub-phases,
`query_mapping` sub-stages, memory samples) — the source for every stage-breakdown table in
RESULTS.md §5. Off by default so normal runs pay none of its cost (`src/profiling.rs`).

### 4.3 `--per-read-stats` + `--per-read-stats-sample` — per-read time and match-count rows

Added most recently (Q1 follow-up), not present upstream. One TSV row per (sampled) read: mapping
time, matches possible/examined, buckets seeded/final, mapq. Written by the same in-order
collector as the PAF, so the file is byte-identical across thread counts for the same reason the
PAF is. Feeds `paper/generated/fig_time_vs_matches` (RESULTS.md §11 records that the scaling
question this was built to answer is not yet cleanly settled by the data).

### 4.4 `-@`/`--threads`

The user-facing surface for Tier 2's threading (`src/params.rs`). `-@1` (the default) still runs
through the same reader/worker/collector pipeline rather than a separate sequential path — with
one worker, completions already arrive in submission order, so the reorder buffer is a no-op, and
there's one fewer code path to keep in sync with the parallel one.

---

## Tier 5 — Kept as-is on purpose, and why

Not every discovered discrepancy from the C++ was fixed. These are the ones deliberately left
alone, with the reasoning that draws the line between "fix" and "port faithfully."

### 5.1 `lost_on_seeding` / `lost_on_pruning` — kept inert, matching the C++

Both are meant to count reads whose true best mapping the seed-heuristic pruning discarded — the
evidence that pruning is safe in practice, not just in the paper's proof. In the C++,
`lost_on_seeding` is a hardcoded `0`, and `lost_on_pruning` is threaded through `match_rest` as an
out-parameter that nothing ever writes to, so its caller always reports `1`. shmap-rs ports both
as the same inert bumps (`src/shmap/scoring.rs:385`, `src/shmap/mod.rs:802`) rather than
"fixing" them to measure something real.

**Why kept, contrasted directly with Tier 1.** Both are self-consistent (they don't grow
unboundedly the way the uncleared-counters bug did) and feed only a diagnostic stat printed to
stderr, never a PAF tag a caller reads. That is the same line §1.1 crossed in the other direction:
output-affecting bugs get fixed, diagnostic-only ones get ported faithfully. Measuring what these
counters were *meant* to measure is listed as future work in RESULTS.md §11 — it needs an
unpruned reference run to diff against, which is a measurement task, not a one-line fix.

### 5.2 Dead CLI flags, kept for compatibility rather than dropped

`-S`/`--max_seeds` and `-n`/`--normalize` are accepted but not read anywhere by the algorithm —
confirmed dead in the C++ too (parsed and printed, never branched on) — and kept as
accepted-but-inert flags so existing invocations don't break, rather than dropped outright
(`src/params.rs:36-46`, `:128-134`). By contrast, `-a`/SAM output is dropped entirely: its
implementation is fully commented out in the C++ with zero live call sites, so there is no
behavior to preserve compatibility with.

### 5.3 The mapq formula matches the C++'s code, not the C++'s comment describing it

The C++ source comments the mapq formula as "similar to minimap2: `mapQ = 40(1-f2/f1)·min(1,m/10)
·log f1`" — but the code beneath that comment implements a simpler all-or-nothing scheme, with two
large alternate formulas commented out nearby, suggesting the tuning was never settled. shmap-rs
implements what the code does, not what the comment claims (`src/mapping.rs:335-345`) — not a
change from the C++'s *behavior*, but worth recording since anyone reading the C++ source's
comments alone would expect different output than either implementation actually produces.

### 5.4 A diagnostic-only counter-ordering fix

`unique_elements_with_info`'s `kmers_notmatched` counter (§7 Seeding) is cosmetic: the C++ resets
its `strike` accumulator to 0 *before* checking whether this k-mer group had any hits, so `nonzero`
always adds 0 and the counter always reports the read's entire sketch size, regardless of how many
k-mers actually matched. Fixed in shmap-rs (`src/shmap/seeding.rs:57`) since it costs nothing and
makes the diagnostic mean what its name says — listed here rather than in Tier 1 because, like
§5.1, it feeds no PAF tag and changes no mapping result.

---

## Summary table

| # | Change | Kind | Affects output? |
|---|---|---|---|
| 1.1 | Per-read counters never cleared | Fix | Yes — live PAF tags |
| 1.2 | Jaccard scores `prev(r)` not `r` | Fix | Yes — Jaccard mappings |
| 1.3 | Single-hit bucket match ignores `segm_id` | Fix | Yes — `bucket_SH` coordinates |
| 1.4 | Unmapped-read UB has no Rust translation | Redesign | Yes — unmapped-read records |
| 1.5 | Unknown-base hash: UB → deterministic 0 | Fix | Only non-ACGT input |
| 2.1 | Multithreaded mapping pipeline | New capability | No (verified byte-identical) |
| 2.2 | Parallel sharded indexing | New capability | No |
| 3.1 | Bucket accumulator (3 generations) | Optimization | No |
| 3.2 | `RefineCache` best/second-best memoization | Optimization | No (verified byte-identical) |
| 3.3 | Packed-key stable sort | Optimization | No (deliberately more deterministic) |
| 3.4 | Rolling-hash rotation tables + bounds-check removal | Optimization | No |
| 3.5 | mimalloc allocator | Optimization | No |
| 3.6 | Smaller wins (chunked sketching, RSS bounding, buffer sizing, LTO, …) | Optimization | No |
| 4.1–4.4 | New CLI capabilities | New capability | No (opt-in, off by default) |
| 5.1 | Inert loss counters | Kept as-is | No |
| 5.2 | Dead CLI flags | Kept as-is | No |
| 5.3 | mapq formula (code vs comment) | Clarification | No — same as C++'s actual behavior |
| 5.4 | `kmers_notmatched` ordering | Fix | Diagnostic only |
