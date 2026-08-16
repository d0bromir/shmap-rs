# Single-thread speed and memory: what changed vs the C++ `shmap` and the map-shmap paper

**Scope.** The changes that make shmap-rs faster or lighter than the C++ reference
implementation and the paper's abstract algorithm (Ivanov & Medvedev, *map-shmap: Practical
long-read mapping with seed heuristic on sketches*) would predict — including, in this revision,
the multithreading and parallel-indexing capabilities the C++ has no analog for at all. Not
covered here: correctness fixes (they change output, not cost — a separate concern) and new
CLI-visible features with no effect on a default run's cost.

**The short version.** shmap-rs preserves shmap's core mapping algorithm — FracMinHash sketching,
rarest-first seeding, seed-heuristic pruning, single/multi-hit index separation, sliding-window
refinement all come from the paper and the C++ unchanged — but substantially redesigns the hot
paths that algorithm runs through, and adds multithreading the C++ has none of.

## Current state, in one table

Every optimization below, current design only. Each row's "Effect, measured" is the figure that
change bought against the build it landed on, and is deliberately *not* refreshed — its value is
that it says what one specific change was worth. Whole-run figures against the current build are in
[`RESULTS.md`](RESULTS.md), regenerated on every suite run. Only row 9 is prescribed by the paper
itself (it names "Optimization 2" directly); everything else is an
engineering choice in how shmap-rs implements the algorithm the paper and the C++ both already
specify, not a change to the algorithm.

| # | Optimization | What it optimizes | Effect, measured | Exact? |
|---|---|---|---|---|
| 1 | Dense bucket array sized by the read's own half-length, not the reference | Peak memory; incidentally fixed a multithreading speed bug (§1) | ~4 MB/worker vs the C++'s constant ~15 GB — the 6.9-7.4x-less-memory headline (RESULTS.md §1) is almost entirely this. On a 6,000-read whole-genome run: `bucket_merge` 1342.1 s → 39.1 s, whole run 1995.6 s → 725.6 s | Yes — byte-identical output; the dense and sparse-fallback paths are tested to agree exactly |
| 2 | Streaming multi-hit seeds — no per-seed scratch hashmap | `match_seeds` speed on repetitive references | Not isolated as its own standalone percentage in the record; replaces ~O(hits) hashmap inserts with O(1) integer work per hit (§2) | Yes — same buckets receive the same accumulated, clamped content |
| 3 | `RefineCache` — the second-best search replays the first's scores instead of recomputing them | `match_rest_for_best2` / `refine` / `query_mapping` speed | 44% of `find_best_mapping` calls eliminated, flat across 1x/3x/10x coverage: `match_rest_for_best2` **-66 to -67%**, `refine` **-39 to -41%**, `query_mapping` **-9 to -11%** (§3) | Yes — byte-identical PAF; restricted to exactly the two metrics (Containment, Jaccard) where it's provably safe — **not** applied to `bucket_SH`/`bucket_LCS` |
| 4 | Multithreaded read mapping — a capability, not present in the C++ at all | Whole-run wall time | RESULTS.md §2 (single-thread and `-@4` per benchmark) and §3 (the full thread-scaling table, including the NUMA ceiling Q4/Q5 diagnosed) | Yes — byte-identical regardless of thread count (`tests/multithreaded_parity.rs`) |
| 5 | Hash-sharded index storage + chunked reference sketching, both parallel across `-@` | Indexing wall time | `index_initializing` **8.4 s → 1.1-1.8 s**; chunked sketching **~18% off `indexing`** at `-@8` (§5) | Yes — index contents never depend on thread count |
| 6 | Two-pass parallel FASTA parsing | Indexing wall time (the reading phase specifically) + peak RSS | `index_reading` **4.4 s → 1.5-1.7 s** (§6) | Yes — `debug_assert`-pinned: pass 2 writes exactly what pass 1 counted, with no gaps |
| 7 | Sketching/index hot loop: precomputed rotation tables, bounds-check-free iteration, `Entry` API, binomial-sized sketch buffers | Reference and read sketching speed | **~13-17% off `index_sketching`** is the bounds-check-free iteration alone (§7); the other three items in this row have no isolated standalone percentage in the record | Yes |
| 8 | Lower allocation/memory traffic: `PMatches` inline for the common single-occurrence case, `Match` borrows its `Seed`, dense `diff_hist`, no `seq` field on `RefSegment`, FASTA records by value, bounded read-ahead, buffered stdout, `lto="fat"` | Peak memory, and speed via fewer allocations | **~11-13% off peak RSS** is the bounded read-ahead alone; **~5% wall** is `lto="fat"` alone (§8); the rest are allocation-*count* reductions (e.g. ~300 M fewer heap allocations for single-occurrence seeds) without an isolated standalone percentage in the record | Yes |
| 9 | Final bucket ordering: an unstable sort on a packed key reproduces stable-sort output — the paper's own Algorithm 4 "Optimization 2" | Sort cost in `get_sorted_buckets`, and (as a side effect) determinism | Sorts 8-byte packed keys instead of 32-byte records — no isolated standalone percentage in the record, but real: moving 4x fewer bytes and skipping a stable sort's temporary allocation | Yes, and stronger than the C++: reproduces exactly what a *stable* sort by descending match count would give (the original index is packed in as the tiebreak), where the C++'s `std::sort` gives no such guarantee even between its own runs (`src/buckets.rs`'s `get_sorted_buckets` doc comment) |

Rows without an isolated standalone percentage are stated as such rather than estimated — they are
real (each is individually documented below with its own reasoning and, where one exists, a code
citation) but were not measured in isolation from the rest of their group at the time they landed.

Two whole-run measurements exist, from two different result sets, and both are real:

- **RESULTS.md's current suite** (five benchmarks, three metrics; see that file's provenance block
  for the commit it was last regenerated from): **2.14–2.97x** faster single-threaded,
  **~7.1x** less peak memory (2.58–2.67 GB vs the C++'s constant 18.85 GB). These two figures move
  with every promoted result set — [RESULTS.md §1](RESULTS.md#1-summary) is authoritative, and this
  paragraph is a summary of it, not a second measurement.
- **An earlier, real-HiFi-whole-genome-at-depth measurement** (commit `f85d9a2`, four coverage
  levels of real HG002 HiFi against the whole genome, single mapping worker): **1.89–2.04x**
  faster, **8.2–9.6x** less peak memory. This lived in a now-consolidated `COMPARISON.md`; its
  numbers are quoted here because they were measured at *depth* (up to 3.17M reads) rather than
  the current suite's smaller per-benchmark counts, which is a meaningfully different regime for
  a mapper whose fixed costs (indexing) amortize differently at different read counts.

Neither supersedes the other — they're different datasets. RESULTS.md is regenerated on every
suite run and is the one to trust for anything current; the depth measurement is kept here
because it's real evidence at a scale the standing suite doesn't currently cover.

**A third measurement, which answers a question neither of those can.** Every "Effect, measured"
above is against *the build that change landed on* — months apart, on different inputs, with
different compilers. Those figures are real, but they share no baseline and they do not sum, so
they cannot say how much of the end-to-end result is which change. That is what
[`benchmarks/scripts/ablation.py`](benchmarks/scripts/ablation.py) exists for: every optimization
here except rows 4 and 8 is switchable back to the code path it replaced at *run time*, in the
shipped binary (`SHMAP_ABLATE`, [`src/ablate.rs`](src/ablate.rs)), so a cumulative ladder can be
measured with one binary, one machine, one compiler and one input, varying one change at a time.
Every rung's PAF is compared byte for byte against the baseline's — a stricter test of the
"Exact?" column than the suite's, because it holds the input fixed and varies only the
optimization — and the run fails rather than reports if one differs. Results and provenance:
[`paper/generated/ABLATION.md`](paper/generated/ABLATION.md).

**A caveat that has to be stated plainly.** The C++ this is measured against is already a tuned
`-O3 -march=native -flto` build, not a naive baseline — every number above is against an
optimized implementation, not a straw man. And the comparison favors shmap-rs more as inputs grow:
the C++ can still win on tiny, startup-dominated inputs (a 101 KB reference finishes in 0.09 s
against shmap-rs's 0.40 s — RESULTS.md §1), while shmap-rs's advantage is a chromosome-scale-and-up
story. Below, "the algorithm" means what the paper specifies and the C++ already implements;
everything under each heading is what changed *on top of* that algorithm, not a replacement for
it.

**Methodology.** Every C++ snippet below is fetched directly from
[`github.com/pesho-ivanov/shmap`](https://github.com/pesho-ivanov/shmap) at commit
[`63f1103`](https://github.com/pesho-ivanov/shmap/tree/63f1103a6e72394fada5f9d9726f4a38f739e8fa)
— the pinned commit this port was checked against — and quoted verbatim with exact line numbers,
not paraphrased from a doc comment. Every Rust snippet is quoted from the file on disk at the time
of writing. A number not attributed to RESULTS.md is the before/after measured when that change
landed, against the build it landed on — organized here around *why* each change exists rather than
*when* it did. The commit that introduced each one carries its own measurement in the message.

**Definitions used throughout**, so each section below doesn't have to re-derive them:

| term | meaning |
|---|---|
| `BucketLoc` | `(segm_id, b)` — which reference segment, and which window index within it |
| `BucketContent` | the accumulated state for one bucket: `matches`, `codirection`, `r_min`/`r_max`, plus `i`/`seeds` propagated from the read |
| `halflen` | half the width of a bucket window; a bucket spans `[b·halflen, (b+2)·halflen)`, so consecutive buckets overlap by half — this is what lets a hit at position `pos` belong to *two* buckets, `pos/halflen` and `pos/halflen − 1` |
| `Kmer` | one sketched k-mer from a sequence: position, hash, strand |
| `Hit` | one occurrence of a k-mer's hash in the *reference*: position, strand, which segment |
| `Seed` | one of a *read's* distinct k-mers, with its reference hit count and every position in the read it occurs at |
| `Match` | a `(Seed, Hit)` pair — one specific seed matched to one specific reference occurrence |

---

## 1. Adaptive bucket accumulation — the largest redesign

**What it is.** `Buckets` (§8 Bucketing) accumulates per-bucket match counts and coordinate
ranges while a read is scored against candidate reference windows. It exists because the paper's
Definition 10 covers a mapping by a *block* (bucket), and every hit needs to be attributed to the
bucket(s) whose window contains it before scoring can happen.

### The C++: a dense array sized by the reference

```cpp
// buckets.h:76-97 (https://github.com/pesho-ivanov/shmap/blob/63f1103a6e72394fada5f9d9726f4a38f739e8fa/src/buckets.h#L76-L97)
template<bool abs_pos>
class Buckets {
	static int    const MAX_SEGMENTS = 100;
	static qpos_t const MIN_HALFLEN  = 5;
public:
	const SketchIndex &tidx;
	qpos_t halflen;
	int i;
	int seeds;
    std::vector<BucketContent> buckets[MAX_SEGMENTS];   // buckets[segm_id][b]
	std::vector<BucketLoc> non_empty_buckets_with_repeats;

	Buckets(const SketchIndex &tidx)
	: tidx(tidx), halflen(-1), i(0), seeds(0) {
		if (tidx.segments() > MAX_SEGMENTS)
			throw std::runtime_error("Number of segments exceeds MAX_SEGMENTS (" + std::to_string(MAX_SEGMENTS) + ")");
		for (int b = 0; b < (int)tidx.segments(); ++b)
			buckets[b].resize(tidx.get_segment(b).sz / MIN_HALFLEN + 2);
	}
```

Every segment gets a slot for every `MIN_HALFLEN`-wide window it could ever be divided into —
`MIN_HALFLEN` is a compile-time constant, `5`, so this is sized for the *smallest bucket width the
algorithm ever allows*, not for what any particular read will actually use. `BucketContent` itself
is `int i; qpos_t seeds, matches; int codirection; rpos_t r_min, r_max;` — six 4-byte fields, 24
bytes, no padding. For a ~3.1 Gbp human genome: `3.1×10⁹ / 5 × 24 bytes ≈ 14.9 GB` — matching the
"~15 GB" figure. This is allocated once and lives for the whole run (there is no threading to
duplicate it across, since the C++ has none — see §4).

Filling it (`add_to_pos`) is a direct indexed write — `buckets[hit.segm_id][b] += content` — but
harvesting the result needs two full sorts, because different hits can touch the same bucket at
different times and `non_empty_buckets_with_repeats` records every touch, duplicates included:

```cpp
// buckets.h:151-174 (https://github.com/pesho-ivanov/shmap/blob/63f1103a6e72394fada5f9d9726f4a38f739e8fa/src/buckets.h#L151-L174)
std::vector< std::pair<BucketLoc, BucketContent> > get_sorted_buckets() {
    std::sort(non_empty_buckets_with_repeats.begin(), non_empty_buckets_with_repeats.end(),
        [](const BucketLoc &a, const BucketLoc &b) {
            if (a.segm_id != b.segm_id) return a.segm_id < b.segm_id;
            return a.b < b.b;
        });
    auto unique_end = std::unique(non_empty_buckets_with_repeats.begin(), non_empty_buckets_with_repeats.end());
    std::vector< std::pair<BucketLoc, BucketContent> > sorted_buckets;
    for (auto it = non_empty_buckets_with_repeats.begin(); it != unique_end; ++it) {
        const BucketLoc& loc = *it;
        sorted_buckets.push_back(std::make_pair(loc, buckets[loc.segm_id][loc.b]));
    }
    std::sort(sorted_buckets.begin(), sorted_buckets.end(), [](const auto &a, const auto &b) {
        return a.second.matches > b.second.matches;
    });
    return sorted_buckets;
}
```

Sort by location to make duplicates adjacent, `std::unique` to collapse them, then a *second* sort
by descending match count — the paper's own Algorithm 4 Optimization 2, and the current-state
table's row 9. Three passes over the touched-bucket list to produce one ordered, deduplicated
result.

**shmap-rs's version of that final sort** — dedup itself works differently by design (below), but
every path funnels into the same descending-match-count ordering before scoring:

```rust
// src/buckets.rs:654-682 (abridged; full comments in the file explain each step)
/// Deduplicates the touched buckets and returns them sorted by
/// descending match count.
///
/// Uses a **stable** sort, unlike the C++'s `std::sort` — ties (equal
/// `.matches`) get a deterministic relative order here, which the C++
/// itself doesn't guarantee even between its own runs/compiler
/// versions. Bit-exact PAF parity against the reference binary isn't a
/// meaningful target specifically for tied buckets as a result; that's
/// a property of the reference implementation, not a port regression.
pub fn get_sorted_buckets(&mut self) -> Vec<(BucketLoc, BucketContent)> {
    self.merge_entries();
    let (i, seeds) = (self.prop_i, self.prop_seeds);

    // Order 8-byte keys, not the 32-byte output records: on a k=15
    // whole-genome read this list is ~240k entries, and sorting it as
    // records moved 4x the bytes and needed the stable sort's temporary
    // allocation. Packing `(u32::MAX - matches, index)` into one `u64`
    // and sorting it ascending reproduces exactly what a *stable* sort by
    // descending `matches` produced — ties keep their original
    // (location-sorted) order — so the result is unchanged, including for
    // the tied buckets the doc comment below calls out.
    self.order.clear();
    self.order.extend(
        self.entries
            .iter()
            .enumerate()
            .map(|(idx, e)| (((u32::MAX - e.matches as u32) as u64) << 32) | idx as u64),
    );
    self.order.sort_unstable();
    // ...
}
```

An *unstable* sort algorithm, but on a key engineered so instability never matters: packing the
original index into the low bits means no two keys are ever truly equal, so `sort_unstable`'s
ambiguity about tie order simply never triggers — the output is exactly what a stable sort by
`matches` alone would give, at an unstable sort's cost. A side effect worth stating plainly: this
makes shmap-rs *more* deterministic than the C++ it's matched against, not less — `std::sort`'s
tie order isn't guaranteed even between two runs of the reference binary itself, which is why
RESULTS.md §2 attributes some fraction of shmap-rs/C++ disagreement to exactly this (adjacent
buckets tied on match count, resolved differently by two different tie-breaking rules).

### shmap-rs: a dense array again, but sized by the read, not the reference

The current design is a dense array like the C++'s, but correctly sized: by the *read's own*
half-length divided into the reference, not by the algorithm's minimum-allowed half-length divided
into the whole genome. A read with half-length `l` partitions a reference of sketch length `n`
into only `n / l` buckets total — for a typical whole-genome HiFi read, ~242k slots, each 16
bytes: **~4 MB, entirely L3-resident**. Three to four orders of magnitude smaller than a
reference-sized array, because it scales with how finely *this one read* divides the reference
rather than with the smallest half-length the algorithm ever allows anyone to request:

```rust
// src/buckets.rs:620-638
pub fn add_to_pos(&mut self, hit: &Hit, content: BucketContent) {
    let b = (if AP { hit.r } else { hit.tpos }) / self.halflen;
    self.merged = false;
    if self.dense_on {
        let g = self.slot(hit.segm_id, b);
        self.dense_add(g, &content);
        if b > 0 {
            self.dense_add(g - 1, &content);
        }
        return;
    }
    // ... sparse fallback below
}

// src/buckets.rs:407-413
#[inline(always)]
fn dense_add(&mut self, g: usize, content: &BucketContent) {
    let slot = &mut self.dense[g];
    if slot.matches == 0 {
        self.touched.push(g as u32);
    }
    slot.add(content);
}
```

`add_to_pos`/`add_to_bucket` are now one indexed read-modify-write, with the touched slot recorded
inline the first time it's hit — no sort, no dedup pass, no per-contribution record. Extraction
then picks the cheaper of two strategies depending on how much of the array a read actually
touched: a whole-genome k=15 read touches nearly every slot, so a linear scan is free and sorting
the (equally long) touched list would be pure overhead; a k=25 read touches a handful, so the
scan's O(slots) cost would dominate and walking just the touched list wins instead:

```rust
// src/buckets.rs:432-441
fn extract_dense(&mut self) {
    // ...
    if touched.len().saturating_mul(3) >= self.dense_slots {
        // scan every slot in order — cheap when most are occupied
    } else {
        // sort and walk just the touched slots — cheap when few are
    }
}
```

Capped at `MAX_DENSE_SLOTS = 2 << 20` — 2,097,152 slots × 16 bytes = **exactly 32 MB per worker**
— as a safety valve for the read that doesn't fit this profile (an unusually small half-length
implies an unusually large slot count), with the sort-based sparse path (below) kept as the
fallback beyond that cap, pinned by a test asserting the two paths agree exactly.

**Measured, on a 6,000-read whole-genome HiFi run at `-@1`:** `bucket_merge` fell from 1342.1 s to
39.1 s, and whole-run wall from 1995.6 s to 725.6 s — byte-identical output.

One more deliberate choice, itself a small stated trade: the array is *not* re-zeroed between
reads. Extraction already clears every slot it takes, so between reads the array is already
all-empty; re-zeroing it anyway would cost O(slots) per read regardless, measured at ~50 s of pure
`memset` across a 240,000-read whole-genome run. The one case that can leave it dirty — a read
abandoned mid-accumulation, before extraction ran — is handled by clearing only that read's own
touched slots.

### How this design was reached

Background, not needed to understand the current design above — kept because it explains a couple
of decisions (why the sparse fallback exists at all, why it's capped rather than trusted
unconditionally) that would otherwise look arbitrary.

Three attempts came before the current one. A dense array sized like the C++'s own (`sz /
MIN_HALFLEN + 2`, the same ~15 GB allocation) was tried first, mirroring the C++ almost exactly —
its one-time allocation-plus-zero-init cost 7-21+ seconds *per worker thread* once threading
existed (§4 below), and was the direct cause of a genuinely counterintuitive bug: multithreaded
whole-genome runs sometimes got *slower* with more threads, because a worker that finished this
huge allocation last simply started with zero reads left to process. Replacing it with
`FxHashMap<BucketLoc, BucketContent>` fixed the memory problem outright — only touched buckets
exist — but cost speed instead: at k=15 on a whole genome, nearly every 15-mer window in a read
matches *somewhere* in a 3+ Gbp reference, so a single read can touch millions of distinct
buckets, and every touch through a hashmap is a full `entry()` call. Measured ~20% slower
single-threaded than the reference-sized array, despite fixing its memory blowup. An append-only
`Vec` of raw contributions, merged once per read by an LSD radix sort on a packed `(segm_id, b)`
key, recovered most of that — already **25% faster than the C++ original** on whole-genome k=15
HiFi at `-@1` (1972.7 s vs 2637.2 s), byte-identical mapped/mapq counts — but still paid to
materialize every raw contribution before collapsing them: a read producing ~4M raw contributions
collapsing to only ~242k distinct buckets meant sorting 4M 32-byte records to do that collapse,
~1.1 GB moved per read at memory-bandwidth speed, 56% of total wall by itself. That sort-based
version is what the current design's dense array replaced for the common case — and what it still
falls back to (`MAX_DENSE_SLOTS`, above) for the read that doesn't fit the dense profile, pinned by
a test asserting the two paths agree exactly.

---

## 2. Streaming multi-hit seeds, instead of a fresh hash map per seed

**What it is.** During `match_seeds`, a seed with more than one reference hit (a "multi-hit"
seed) needs its hits folded into buckets, with a cap: a bucket's contribution from one seed is
`min(hits in that bucket, occurrences of the seed in the read)`, so counting can't simply happen
per-hit — it needs to be aggregated per-bucket first.

### The C++: a fresh `BucketsHash` per seed

```cpp
// shmap.h:105-140 (https://github.com/pesho-ivanov/shmap/blob/63f1103a6e72394fada5f9d9726f4a38f739e8fa/src/shmap.h#L105-L140)
void match_seeds(const Seeds &p_unique, BucketsType &B, qpos_t S) {
    for (; B.i < (qpos_t)p_unique.size() && B.seeds < S; B.i++) {
        Seed seed = p_unique[B.i];
        if (seed.hits_in_T > 0) {
            if (seed.hits_in_T == 1) {
                const auto &hit = tidx.h2single.at(seed.kmer.h);
                BucketContent content(1, 0, hit.strand == seed.kmer.strand ? 1 : -1, hit.r, hit.r);
                B.add_to_pos(hit, content);
            } else {
                BucketsHash<abs_pos> b2m(B.halflen);
                for (const auto &hit: tidx.h2multi.at(seed.kmer.h)) {
                    BucketContent content(1, 0, hit.strand == seed.kmer.strand ? 1 : -1, hit.r, hit.r);
                    b2m.add_to_pos(hit, content);
                }
                for (auto it = b2m.buckets.begin(); it != b2m.buckets.end(); ++it) {
                    BucketContent content(min(it->second.matches, seed.occs_in_p), 0, it->second.codirection, it->second.r_min, it->second.r_max);
                    B.add_to_bucket(it->first, content);
                }
            }
        }
    }
}
```

`BucketsHash<abs_pos> b2m(B.halflen)` is a fresh scratch structure — `ankerl::unordered_dense::map
<BucketLoc, BucketContent, ...>` — constructed for *every multi-hit seed*, populated by calling
`add_to_pos` (a hashmap insert, twice per hit: `buckets[b]` and `buckets[b-1]`) for every one of
that seed's reference hits, then walked once to fold its results into the real accumulator `B`.
For a seed with a million hits, that's a million-entry hashmap built and torn down, purely as
scratch space to compute one `min(...)` clamp per bucket.

### shmap-rs: exploit that the hits are already sorted

```rust
// src/shmap/seeding.rs:106-165 (abridged; full comments in the file explain each step)
} else {
    // Streaming replacement for the per-seed `BucketsHash`:
    // `h2multi[h]` is sorted by `(segm_id, r)`, and within a
    // segment `r`/`tpos` increase together, so the bucket
    // index `pos/halflen` is monotonically non-decreasing
    // across this seed's hits. Each hit only touches buckets
    // `b` and `b-1`, so a bucket is final once we reach a hit
    // two buckets ahead.
    let occs = seed.occs_in_p;
    let mut cur_sid: SegmId = -1;
    let mut b: RPos = 0;
    let mut b_hi: RPos = 0;
    // The only two buckets that can still receive a contribution,
    // held as fixed slots for `base` and `base + 1`.
    let mut base: RPos = 0;
    let mut acc = [BucketContent::default(); 2];
    for hit in self.tidx.multi_hits(seed.kmer.h) {
        let pos = if AP { hit.r } else { hit.tpos };
        let content =
            BucketContent::new(1, 0, if hit.strand == seed.kmer.strand { 1 } else { -1 }, hit.r, hit.r);
        if hit.segm_id != cur_sid {
            // New segment: everything buffered is final.
            for (j, a) in acc.iter_mut().enumerate() {
                if a.matches > 0 {
                    flush_slot(buckets, cur_sid, base + j as RPos, a, occs);
                    *a = BucketContent::default();
                }
            }
            cur_sid = hit.segm_id;
            b = pos / halflen;
            b_hi = (b + 1) * halflen;
            base = (b - 1).max(0);
        } else {
            // Advance `b` to the bucket containing `pos` by comparison,
            // not division — recomputed by division only on this
            // segment change, an amortized-O(1) replacement for a
            // per-hit integer divide over billions of hits.
            while pos >= b_hi {
                b += 1;
                b_hi += halflen;
            }
            // Slide the two-slot window up to the new `base`, finalizing
            // whatever falls out the bottom — one slot if `base` advanced
            // by exactly one bucket (the common case), both if it jumped
            // further (a gap with no hits in between).
            let new_base = (b - 1).max(0);
            if new_base > base {
                if acc[0].matches > 0 {
                    flush_slot(buckets, cur_sid, base, &acc[0], occs);
                }
                if new_base == base + 1 {
                    acc[0] = acc[1];
                } else {
                    if acc[1].matches > 0 {
                        flush_slot(buckets, cur_sid, base + 1, &acc[1], occs);
                    }
                    acc[0] = BucketContent::default();
                }
                acc[1] = BucketContent::default();
                base = new_base;
            }
        }
        // A hit touches both bucket b and b-1 (halflen-overlapping windows).
        acc[(b - base) as usize] += content;
        if b > 0 {
            acc[(b - 1 - base) as usize] += content;
        }
    }
    // Flush whatever is still buffered after the seed's last hit.
    for (j, a) in acc.iter().enumerate() {
        if a.matches > 0 {
            flush_slot(buckets, cur_sid, base + j as RPos, a, occs);
        }
    }
}
```

The insight is the same sortedness the C++'s own index already guarantees (`h2multi[h]` sorted by
`(segm_id, r)`) — shmap-rs's version just *uses* it instead of re-deriving structure with a
hashmap. Because a hit only ever touches bucket `b` and `b-1`, and `b` is monotonically
non-decreasing within a segment, at most two buckets can be "live" at any point while streaming
through a seed's hits — held as two fixed array slots, `acc[0]`/`acc[1]`, rather than a growable
map. A bucket is flushed (finalized and folded into the real accumulator, with the `min(...,
occs)` clamp applied) the moment the stream moves past it. Bucket-index computation itself avoids
a division per hit too: since `pos` only increases within a segment, `b` is advanced by comparing
against a precomputed upper bound (`b_hi`) instead of recomputing `pos / halflen` every time —
division is paid only once per segment change.

**Why this is `match_seeds`'s dominant cost on repetitive references**, per the source's own
comment: this replaces the ~O(hits) `FxHashMap` inserts the scratch `BucketsHash` did with O(1)
integer work per hit. Output is unchanged — the same set of buckets receives the same accumulated,
clamped content, in whatever order `Buckets` re-derives results in, which doesn't depend on
insertion order.

---

## 3. Memoizing the second-best search against the best-mapping search

**What it is.** `match_rest` (§10 Scoring; the paper's Algorithm 1) finds a read's best mapping,
then searches again for the second-best — needed to compute mapq, Definition 6 — over the same
surviving buckets with a lower-bound cutoff.

### The C++: two full, separate searches

```cpp
// shmap.h:508-520 (https://github.com/pesho-ivanov/shmap/blob/63f1103a6e72394fada5f9d9726f4a38f739e8fa/src/shmap.h#L508-L520)
int lost_on_pruning = 1;
std::optional<Mapping> best, best2;
T.start("match_rest");
    T.start("match_rest_for_best");
        best = match_rest(P.size(), m, lmax, p_unique, B, sorted_buckets, diff_hist, p_ht, theta, std::nullopt, query_id, &lost_on_pruning, params.max_overlap);
    T.stop("match_rest_for_best");
    T.start("match_rest_for_best2");
        if (best) {
            double second_best_thr = best->score() * (1.0 - params.min_diff);
            best2 = match_rest(P.size(), m, lmax, p_unique, B, sorted_buckets, diff_hist, p_ht, second_best_thr, best, query_id, &lost_on_pruning, params.max_overlap);
        }
    T.stop("match_rest_for_best2");
T.stop("match_rest");
```

Two calls into `match_rest`, over the identical `sorted_buckets` list, differing only in the
threshold and which mapping (if any) to exclude. The paper's Algorithm 1 calls `slideChain` once
per search without discussing whether the two searches can share work — and as written, they
don't: every bucket both searches visit gets scored from scratch twice.

### shmap-rs: the second search is a pure function of bucket location — on two of the four metrics

**The discovery, precisely.** On `Containment`/`Jaccard` specifically, `find_best_mapping` turns
out to be a *pure* function of a bucket's location alone: it never reads the mutable `content`
that pruning updates between the two searches, and it restores its own scratch state (`diff_hist`)
exactly as it found it — which is exactly what `best_fixed_length`'s closing
`debug_assert_eq!(intersection, 0)` exists to pin down. So the second sweep was recomputing
bit-identical scores for every bucket the first sweep had already scored. This is specifically
*not* true for `bucket_SH`/`bucket_LCS`: both build their result directly out of `content`, the
same mutable state pruning changes between sweeps — so the cache is restricted to exactly the two
metrics where it's provably safe:

```rust
// src/shmap/scoring.rs:399-401
// `find_best_mapping` is only a pure function of the bucket location
// on the fixed-length metrics; `BucketSh`/`BucketLcs` both build
// their result out of `content`, which the pruning pass mutates
// between the two sweeps, so they must not be memoized.
let memoizable = matches!(metric, Metric::Containment | Metric::Jaccard);
```

`RefineCache` records the first sweep's results and replays them in the second; because both
sweeps walk the identical `sorted_buckets` slice in the identical order, replay is a monotone
cursor over one reusable `Vec` — no hashing, no lookup structure at all.

**Not a full hit by construction.** The first pass's acceptance threshold ratchets upward as its
own best score improves, so a bucket the first pass discards late can still clear the second
pass's flatter cutoff. Those are genuine misses, recomputed from scratch — which is exactly why
the measured win is 44%, not 100%.

**Measured.** 44% of `find_best_mapping` calls eliminated, flat across coverage (1x/3x/10x) on
real HG002 HiFi against the whole genome at `-@1`: `match_rest_for_best2` −66.4%/−67.1%/−66.5%,
`refine` −39.2%/−41.3%/−39.8%, `query_mapping` −9.6%/−10.5%/−9.4%. PAF output byte-identical;
`SHMAP_NO_REFINE_MEMO=1` disables it for a direct A/B on one binary.

---

## 4. Multithreaded read mapping

**What it is.** Mapping every read against the built index — the phase that dominates wall time
once the reference is indexed.

**In the C++:** entirely serial. `map_reads` loops over the input FASTA and calls `map_read` once
per record, with no threading construct anywhere in the mapping path — confirmed by grep, zero
matches.

**In shmap-rs:** `map_reads` runs a fixed three-stage pipeline over `std::thread::scope`:

- one reader thread streams records off disk and dispatches them as `Job`s over a bounded channel
  (bounding memory to a few jobs ahead of the workers, not the whole file);
- `-@` worker threads each own an independent `SHMapper` + `Buckets` — per-read scratch can't be
  shared, which is exactly why §1's redesign had to happen before this could be affordable — and
  turn each job into a `ReadOutput`, tagged with its original sequence index;
- the scope's own thread is the sole collector: it reorders completions by index and applies them
  strictly in input order.

```rust
// src/shmap/mod.rs — module doc comment, "Multithreading" section
// Not present upstream at all (grep confirms zero threading in the C++).
// one reader thread streams records off disk via `read_fasta` and
// dispatches them as `Job`s over a bounded channel (bounding memory to
// a few jobs ahead of the workers);
// `params.threads.max(1)` worker threads each own an independent
// `SHMapper` + `Buckets` (per-read scratch state can't be shared across
// threads) and turn each `Job` into a `ReadOutput`, sent back tagged
// with its original sequence index;
// the scope's own thread (no extra thread for this part) is the sole
// collector: it reorders completions by index and applies them
// strictly in input order, so stdout/PAF/`.unmapped.paf`/`paul.tsv`
// output is byte-identical regardless of thread count.
```

**Why determinism, specifically.** Output — stdout, the PAF, `.unmapped.paf`, `paul.tsv` — is
byte-identical at every thread count because the collector applies strictly in submission order;
only the CPU-bound mapping work parallelizes. Verified by a dedicated test
(`tests/multithreaded_parity.rs`), and it's what makes any thread-scaling comparison meaningful at
all — a mapper whose output changed with thread count couldn't be benchmarked this way in the
first place.

**Robustness, layered on top.** Each worker catches panics from its own `map_read` call rather
than letting one bad read kill the thread — without this, a dead worker stops draining the bounded
channel, the reader blocks trying to send into it, and the main thread blocks forever joining it,
turning one bad read into a permanent hang. Found by reproducing exactly this hang (`-v 2` against
reads without ground-truth-encoded headers, which panics by documented design).

**`-@1` is not a separate code path** — it runs through the identical pipeline, paying none of its
threading overhead and gaining none of its benefit: with a single worker, completions already
arrive in submission order, so the reorder buffer is a no-op.

---

## 5. Parallel reference indexing

**What it is.** Building `SketchIndex` — sketching every reference segment and populating the
hash table that maps a k-mer hash to its reference occurrence(s) — once per run, before any read
is processed.

**In the C++:** entirely single-threaded.

**In shmap-rs:** two techniques combine.

**Hash-sharded index storage: `index_initializing` 8.4 s → 1.1–1.8 s.** The k-mer hash table is
split into `N_SHARDS = 8` independent shards, each its own `HashMap`. Every occurrence of a given
hash lands in the same shard by construction (`shard_of`, a pure function of the hash), so the
parallel fill needs no locking and cannot depend on scheduling. Fixed at 8 regardless of `-@`, so
the index's *contents* never depend on thread count. Three design choices had to be right, each
found by measuring a wrong first attempt:

- *Shard on the low hash bits, against the usual advice.* FracMinHash keeps only hashes below
  `h_frac · u64::MAX`, so every hash reaching the index is small — at `-r 0.01`, under `2^57`,
  with the top 7 bits always zero. Sharding on the high bits (normally the best-mixed choice)
  puts every k-mer in shard 0 and silently serializes the whole build: an early version did
  exactly this with 64 shards, and it cost 6.96 s on one thread while the other 63 finished in
  0.13 s.
- *Keep the fill interleaved with reading at `-@1`.* Deferring inserts to a separate phase loses
  the overlap the collector already had with the reader and sketcher: 9.2 s → 15.1 s, slower.
- *Hold the shards in a fixed-size array, not a `Vec`.* Every index probe in the mapping hot path
  goes through this array; the extra dependent load through a `Vec`'s indirection cost
  `collect_kmer_info` up to 24%.

```rust
// src/index.rs:73-74
const SHARD_BITS: u32 = 3;
pub const N_SHARDS: usize = 1 << SHARD_BITS;
```

**Why 8 shards, not more.** More shards cost real money elsewhere: every index probe in the
*mapping* hot path indexes this array, and a wider one is colder. Measured on real HiFi at 10x
`-@1`, where mapping is ~98% of the wall, 64 shards cost 3.4% overall while 8 cost 0.7%.

**Sketching a segment is split into chunks, not one segment per worker.** A k-mer window depends
only on the `k` bases under it, so `sketch_slice_into` can sketch an offset range of one segment
and the pieces concatenate to exactly the serial sketch — removing the Amdahl cap where indexing
couldn't finish faster than sketching the single longest chromosome. ~18% off `indexing` at `-@8`.

---

## 6. Two-pass parallel FASTA parsing

**What it is.** Reading the reference FASTA and splitting it into per-segment nucleotide buffers,
before any sketching happens.

**Where the cost was.** Reading isn't I/O-bound — the 3.18 GB human reference streams off page
cache at 3.7 GB/s, 0.87 s total — so the real cost is line-splitting, newline-stripping, and
copying bases into segment buffers.

**Why one pass isn't enough.** A first parallel design split the file into 16 MB byte ranges
parsed by up to 8 workers in a single pass, but instrumenting it found the win was capped by the
*collector*: workers spent only 0.05 s waiting to hand off results, while 2.8–3.2 s of a 2.9–3.2 s
total read went into concatenating those ranges into a growing per-segment buffer on one thread —
not the memory copy itself, but the doubling reallocations and ~780k serialized first-touch page
faults that come with growing a buffer incrementally.

**The fix:** two passes. Pass 1 only *counts*, walking the same byte ranges in parallel to
determine every segment's exact final size and every range's exact offset within it; pass 2 lets
worker threads write straight into disjoint slices of a buffer that's already the right size —
zero reallocation, first-touch page faults spread across threads instead of serialized on one.
Both passes drive the same line-walking logic so they cannot disagree about where a line boundary
falls, and two `debug_assert`s pin that pass 2 writes exactly what pass 1 counted and that a
segment's parts tile its buffer with no gaps.

```rust
// src/io/mod.rs — read_fasta_parallel's doc comment
// Reading was the last serial phase of indexing. It is not I/O bound —
// the 3.18 GB human reference streams in 0.87 s at 3.7 GB/s — so the
// cost is line splitting, newline stripping and copying, and that
// parallelises. The two passes exist to keep the *copy* parallel too.
```

**Measured:** `index_reading` 4.4 s → 1.5–1.7 s (`fasta_scan` ~0.3 s + `fasta_fill` ~1.3 s). Falls
back to the original single-pass reader for compressed input, small files, `-@1`, and non-Unix
targets — behavior is unchanged wherever the split doesn't apply. A side effect: peak RSS falls
slightly too, since a reallocating growth strategy transiently holds both the old and new buffer
during a copy, and sizing the buffer exactly once removes that overhead entirely rather than
shrinking it.

---

## 7. Sketching and index hot-loop improvements

**What it is.** `FracMinHash::sketch_slice_into` (§5 Sketching) computes a forward and
reverse-complement ntHash-family rolling hash per k-mer window — the single hottest loop in the
mapper, run once per base of the reference and once per base of every read.

### Precomputed rotation tables: 3 of 5 per-base rotates removed

```cpp
// sketch.h:461-463 (https://github.com/pesho-ivanov/shmap/blob/63f1103a6e72394fada5f9d9726f4a38f739e8fa/src/sketch.h#L461-L463)
h_fw = std::rotl(h_fw, 1) ^ std::rotl(LUT_fw[ size_t(s[r-k]) ], k) ^ LUT_fw[ size_t(s[r]) ];
h_rc = std::rotr(h_rc, 1) ^ std::rotr(LUT_rc[ size_t(s[r-k]) ], 1) ^ std::rotl(LUT_rc[ size_t(s[r]) ], k-1);
```

Count them: `rotl(h_fw, 1)`, `rotl(LUT_fw[...], k)`, `rotr(h_rc, 1)`, `rotr(LUT_rc[...], 1)`,
`rotl(LUT_rc[...], k-1)` — **five rotates per base.** The C++ already precomputes the *base* hash
per character into `LUT_fw`/`LUT_rc` (256-entry tables filled once, at the four ACGT slots), but
three of these five rotates are applied to a *table lookup result*, freshly, on every single base
— even though the rotate amount (`k`, `1`, or `k-1`) never changes for the life of the run, since
`k` is fixed.

```rust
// src/sketch.rs:37-52
/// Per-base contributions with the fixed rotates the rolling update
/// applies to the *outgoing*/*incoming* base baked in, so the hot loop
/// does a plain table load instead of a load+rotate each. `lut_fw_k[c]
/// = lut_fw[c].rotate_left(k)`, `lut_rc_r1[c] = lut_rc[c].rotate_right(1)`,
/// `lut_rc_k1[c] = lut_rc[c].rotate_left(k-1)`.
lut_fw_k: [Hash; 256],
lut_rc_r1: [Hash; 256],
lut_rc_k1: [Hash; 256],
```

shmap-rs precomputes those same three rotated values *into three additional tables*, once per
`FracMinHash` instance, so the rolling loop does a plain table load instead of a load-then-rotate
— removing 3 of the 5 rotates across every base of the reference and every read. Tried and
rejected: interleaving each base's forward/reverse pair into one `[Hash; 2]` table to halve the
load count, measured ~6% *slower* instead — the 16-byte load goes through a vector register and
has to be split apart again before the scalar XORs.

### Bounds-check-free iteration

The update used to index the sequence at `s[r]` and `s[r - k]` through a signed `RPos`, costing
two bounds checks plus a sign-extension per base, with a mid-loop `r >= s.len()` break stopping
LLVM from recognizing the loop as counted at all. Walking the incoming and outgoing bases as a
pair of zipped slice iterators lets the compiler prove the indices are always in range from the
iterators' lengths alone: **~13–17% off `index_sketching`.**

### `Entry` instead of `contains_key` + `insert`: one hash instead of two

```rust
// src/index.rs:264-286
#[inline]
fn insert_hit(shard: &mut Shard, kmer: &Kmer, tpos: RPos, segm_id: SegmId, max_matches: Option<i32>) {
    let hit = Hit::new(kmer, tpos, segm_id);
    // `entry` instead of `contains_key` + `insert`/`entry`: hashes
    // `kmer.h` once instead of twice for the common (single-hit)
    // case — this loop runs once per indexed k-mer (~19M for the
    // full CHM13 genome), so the redundant hash was a real,
    // measurable cost in `index_initializing`.
    match shard.h2single.entry(kmer.h) {
        std::collections::hash_map::Entry::Vacant(e) => {
            e.insert(hit);
        }
        std::collections::hash_map::Entry::Occupied(_) => {
            // `with_capacity(2)`, not `or_default()`: a k-mer that
            // reaches `h2multi` at all ends up holding at least two
            // hits, and most hold exactly two. Starting empty made
            // that near-universal case allocate at capacity 1 and
            // then immediately reallocate — millions of avoidable
            // reallocations on a whole genome, for the same final memory.
            let multi = shard.h2multi.entry(kmer.h).or_insert_with(|| Vec::with_capacity(2));
            if max_matches.is_none_or(|m| (multi.len() as i32) < m + 1) {
                multi.push(hit);
            }
        }
    }
}
```

A `contains_key` check followed by a separate `insert` call hashes the key twice — once to check,
once to insert. `entry()` hashes once and returns a handle to the slot either way. Over ~19M
indexed k-mers on the full CHM13 genome, that redundant hash was a real, measurable cost.

### Sketch buffer capacity from the actual distribution

Selection is a Bernoulli trial per k-mer, so the count of selected k-mers has mean `len · h_frac`
and standard deviation `√mean`. shmap-rs reserves `mean + 6σ` instead of a flat `1.1x` — leaving
the chance of a mid-sketch reallocation astronomically small while costing only ~0.5% slack on a
whole chromosome, against the ~10% a flat factor spends unconditionally on every sequence
regardless of its actual variance. Overflow stays merely slow, never wrong.

**Combined effect of the hot-loop changes:** roughly **13–17% off reference sketching time.**

---

## 8. Lower allocation and memory traffic

Several smaller structural choices, each removing an allocation or a copy that the C++ pays for on
every read or every k-mer.

### A single query position stored inline, not in a one-element vector

```rust
// src/types.rs:74-81
/// The query positions a seed's k-mer occurs at, sorted decreasing.
///
/// Stored inline when there is exactly one, which is the overwhelmingly
/// common case: on real HiFi reads ~96% of a read's k-mers occur exactly once
/// in it (310 M k-mers against 299 M distinct, measured on a 10x whole-genome
/// run). Building this as a `Vec` per seed meant ~300 M heap allocations for
/// a single `i32` each.
pub enum PMatches {
    One(QPos),
    Many(Vec<QPos>),
}
```

The C++'s `Seed::pmatches` is unconditionally `std::vector<qpos_t>` — even a seed occurring once
in the read allocates a one-element heap vector to record that. `PMatches::One(QPos)` stores that
overwhelmingly common case inline in the enum itself, with no heap allocation at all; only the
~4% of seeds with genuine repeats fall back to `Many(Vec<QPos>)`.

### `Match` borrows its `Seed`; the C++ copies the whole thing, heap vector included

```cpp
// types.h:43-71 (https://github.com/pesho-ivanov/shmap/blob/63f1103a6e72394fada5f9d9726f4a38f739e8fa/src/types.h#L43-L71)
struct Seed {
	Kmer kmer;
	rpos_t hits_in_T;
	qpos_t occs_in_p;
	qpos_t seed_num;
	std::vector<qpos_t> pmatches;   // positions in `p' of all occurences of `kmer'
	// ...
};

struct Match {
	Seed seed;   // owned by value
	Hit hit;
	Match(const Seed &seed, const Hit &hit)
		: seed(seed), hit(hit) {}   // copies the Seed, including its heap-allocated pmatches
};
```

Every `Match` the C++ constructs deep-copies a full `Seed` — kmer, counts, and the heap-allocated
`pmatches` vector all over again — even though the seed already exists elsewhere and outlives the
match. shmap-rs's `Match` borrows the `Seed` it refers to instead of owning a copy, since nothing
about a match needs the seed to outlive the borrow.

### `diff_hist` is a dense `Vec` indexed by `seed_num`, on the path that matters

```rust
// src/shmap/mod.rs:659-663
let mut diff_hist: Vec<QPos> = vec![0; p_unique.len()];
for seed in &p_unique {
    diff_hist[seed.seed_num as usize] = seed.occs_in_p;
}
```

On `match_rest`'s actual hot path (`best_fixed_length`, `find_best_mapping` — every normal read),
`diff_hist` is a plain `Vec<QPos>` indexed directly by `seed_num`, not a hash map — a read has at
most a few thousand seeds, `seed_num` is already dense and contiguous by construction, so a vector
index replaces a hash-and-probe with a direct offset. (A second, hashmap-keyed `diff_hist: H2Cnt`
does exist in `refine.rs`'s `Matcher`, but it backs only the optional ground-truth diagnostic path
in `analyse_simulated.rs` — off by default, and not part of normal mapping.)

### The full reference sequence is discarded after sketching

```cpp
// sketch.h:20-27 (https://github.com/pesho-ivanov/shmap/blob/63f1103a6e72394fada5f9d9726f4a38f739e8fa/src/sketch.h#L20-L27)
struct RefSegment {
	sketch_t kmers;
	std::string name;
	std::string seq;   // empty if only mapping and no alignment
	rpos_t sz;
	int id;
};
```

```cpp
// index.h:104 (https://github.com/pesho-ivanov/shmap/blob/63f1103a6e72394fada5f9d9726f4a38f739e8fa/src/index.h#L104)
T.push_back(RefSegment(sketch, segm_name, segm_seq, segm_seq.size(), T.size()));
```

The comment says `seq` is "empty if only mapping and no alignment" — but the constructor call that
actually builds the index passes `segm_seq`, the real sequence content, *unconditionally*, so the
field is populated with the full reference every time an index is built regardless. Searching the
mapping code for anywhere `.seq` is actually read finds exactly two lines, both fully commented
out:

```cpp
// shmap.h:433, 440 — both dead
//const char *P = seq->seq.s;
//qpos_t P_sz = (qpos_t)seq->seq.l;
```

So the C++ carries a second, full-size copy of the genome in memory for the entire run, for a
consumer that doesn't exist in live code. shmap-rs's `RefSegment` has no `seq` field at all — only
the sketch survives past indexing:

```rust
// src/sketch.rs:9-17
/// The C++ `RefSegment` also stores the segment's full nucleotide sequence
/// (`seq`), but that field is only ever read by the fully-commented-out
/// SAM/edlib alignment code — carrying it here would roughly double index
/// memory for a feature that's dead code upstream, so it's dropped.
pub struct RefSegment {
    pub kmers: SketchT,
    pub name: String,
    pub sz: RPos,
    pub id: i32,
}
```

### The rest, briefly

- **FASTA records handed to the caller by value**, removing a second full copy of every
  chromosome that both call sites used to make with `.to_vec()`, since the underlying reader
  already returns an owned buffer.
- **A segment's read-ahead is bounded to its own chunks**, not `threads * 4` chunks, so its memory
  frees as soon as it's sketched rather than staying alive until the (slower) index-apply step —
  ~11–13% off peak RSS on indexing-dominated runs.
- **Buffered stdout in the collector** — one `BufWriter` held for the whole run instead of
  `print!()` per read, which flushes (a syscall) on every trailing newline.
- **`lto = "fat"` + `codegen-units = 1`** in the release profile — worth ~5% wall on its own,
  purely from cross-crate inlining across the `needletail`/`rustc-hash` boundary that the default
  16 codegen units prevented. `panic = "unwind"` stays as-is (not `"abort"`) because the per-read
  panic isolation in §4 needs `catch_unwind`.
- **`h2multi`'s lists are shrunk once built, but only where the slack pays for the copy** (`> len/8
  + 8`). They are read-only after indexing, so the final sort pass can shrink them — but shrinking
  the two- and three-hit lists that dominate by *count* measured a net loss (~5.8 M reallocations to
  recover ~100 MB), while the rare very-high-frequency k-mers that dominate by *bytes* clear the
  threshold easily.

**The sparse fallback path carries four more**, each measured on its own before the dense
accumulator (§1) displaced it as the primary path. They still run for short reads and for any read
over `MAX_DENSE_SLOTS`:

- The radix key packed `segm_id << 32`, forcing a 37-bit key and a third O(n) pass where the real
  key is ~22 bits. Measuring the width per read instead cut it to two passes — **−30%**.
- The sorted entry shrank **32 → 24 bytes** by hoisting `BucketContent`'s `i`/`seeds`, which are
  uniform across a read, out of the per-entry payload.
- All passes' histograms are built in **one** counting scan rather than one per pass, since a
  histogram is permutation-invariant.
- The final ordering by `matches` sorts 8-byte packed keys rather than 32-byte records (row 9).

---

## Why `-@1` is the number to quote for memory specifically

Almost every technique in §1 lives inside per-worker state. At `-@1` there is exactly one
`Buckets` instance, so the ~7.1x (or 8.2–9.6x, on the depth measurement) memory ratio is a
clean single-instance comparison against the C++'s one process. RESULTS.md §3c shows this ratio
narrows at higher thread counts precisely because it *is* per-worker: `N` threads hold `N` copies
of a data structure that individually became tiny, and tiny times many is no longer tiny. That's
parallelism multiplying a fixed per-worker cost, not a regression in the redesign — the per-worker
cost genuinely fell by three orders of magnitude; it just still multiplies.

---

## Not covered here

- **Correctness fixes** — five places where output itself differs from the C++ or the paper (an
  uncleared-counters bug corrupting two live PAF tags across a run, a Jaccard scoring off-by-one
  the C++'s own author flagged as unresolved, a bucket-coordinate bug, and two cases of undefined
  C++ behavior with no faithful Rust translation). These change *what* gets computed, not its
  cost.
- **New CLI-visible capabilities** — research knobs for repeat-region accuracy, profiling
  instrumentation — all off by default, none changing the cost of a default run.
- **Approaches that were tried and rejected**, with the measurement that rejected them — SIMD, NUMA
  index replication, software prefetching, 2-bit packing and the rest are in
  [RESULTS.md §11](RESULTS.md#11-what-to-try-next), which is the single home for negative results.
