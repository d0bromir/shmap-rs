//! Position-bucketed candidate-region accumulators.
//!
//! Port of `shmap/src/buckets.h`.
//!
//! `AP` (the C++ `abs_pos` template bool) selects whether bucket indices
//! are computed from a hit's absolute reference position (`hit.r`) or its
//! index into the reference sketch (`hit.tpos`).

use rustc_hash::FxHashMap;

use crate::index::SketchIndex;
use crate::types::{BucketContent, BucketLoc, Hit, QPos, RPos, SegmId};

/// Smallest allowed bucket half-length.
pub const MIN_HALFLEN: QPos = 5;

/// Widest digit [`Buckets`]'s LSD radix sort will use.
///
/// The digit width actually used is chosen per read (see
/// `radix_sort_entries`): once the pass count is fixed by the measured key
/// width, the digit is narrowed to spread the key evenly over those passes.
/// Going from one 16-bit digit to two 11-bit digits for the same 22-bit key
/// costs nothing in passes but shrinks the histogram from 256 KB to 8 KB (L1
/// rather than L2) and cuts the number of simultaneously-open scatter output
/// streams from 65 536 to 2 048, which is what the write path actually cares
/// about. An earlier note here recorded narrower digits measuring *worse* —
/// that was with the old `segm_id << 32` key, where narrowing genuinely added
/// passes rather than rebalancing existing ones.
const RADIX_MAX_BITS: u32 = 16;
/// A packed `(segm_id, b)` key is at most 64 bits; this many `RADIX_MAX_BITS`
/// digits cover that worst case.
const RADIX_MAX_PASSES: u32 = 64 / RADIX_MAX_BITS;

/// Packs a [`BucketLoc`] into a sort key that orders the same way as
/// comparing `(segm_id, b)` lexicographically, given that every `b` in the
/// data fits in `b_bits` bits. Both fields are always non-negative (segment
/// indices and `pos / halflen` bucket indices), so the `i32 -> u32`
/// bit-reinterpretation preserves ordering.
///
/// `b_bits` is measured per read rather than fixed at 32. That single change
/// usually removes an entire radix pass: `b` is a reference position divided
/// by a read-scaled half-length, so it needs ~17 bits on a human genome, and
/// `segm_id` needs ~5 — 22 bits of real key, which fits in two 16-bit digits.
/// Hardcoding `segm_id << 32` pushed the key to 37 bits and forced a third
/// full O(n) pass over data that is already the dominant cost of mapping.
#[inline(always)]
fn radix_key(loc: &BucketLoc, b_bits: u32) -> u64 {
    debug_assert!(loc.segm_id >= 0 && loc.b >= 0);
    ((loc.segm_id as u32 as u64) << b_bits) | (loc.b as u32 as u64)
}

/// Largest dense accumulator a single read may allocate, in slots (16 bytes
/// each, so ~32 MB per worker thread).
///
/// The dense path's footprint is `reference_sketch_len / halflen`, and
/// `halflen` is the read's own k-mer count — so it grows as reads get
/// *shorter*. Long-read whole-genome runs, the case this exists for, land
/// around 240k slots (~4 MB); this cap is what keeps a short-read workload
/// from reproducing the multi-GB dense-array blowup that the sparse path was
/// originally written to fix. Above it, `Buckets` silently uses the sparse
/// path instead, which is slower but bounded by what a read actually touches.
const MAX_DENSE_SLOTS: usize = 2 << 20;

/// A dense accumulator slot: a [`BucketEntry`] minus its location, which is
/// implied by the slot's index. `matches == 0` means untouched — every
/// contribution carries at least one match.
#[derive(Clone, Copy)]
struct DenseSlot {
    matches: QPos,
    codirection: i32,
    r_min: RPos,
    r_max: RPos,
}

const EMPTY_SLOT: DenseSlot = DenseSlot {
    matches: 0,
    codirection: 0,
    r_min: RPos::MAX,
    r_max: -1,
};

impl DenseSlot {
    #[inline(always)]
    fn add(&mut self, c: &BucketContent) {
        self.matches += c.matches;
        self.codirection += c.codirection;
        self.r_min = self.r_min.min(c.r_min);
        self.r_max = self.r_max.max(c.r_max);
    }
}

/// One accumulated contribution to one bucket.
///
/// Deliberately 24 bytes rather than the 32 that a `(BucketLoc,
/// BucketContent)` pair takes. `merge_entries` reads and rewrites this struct
/// several times per radix pass over ~4M entries per read on a whole-genome
/// k=15 run, and that traffic — measured at ~1.1 GB per read, running at
/// memory-bandwidth speed — is the single largest cost in the mapper. Width
/// here converts almost linearly into wall time.
///
/// `BucketContent`'s `i` and `seeds` are not carried per entry because they
/// are uniform across a read: `propagate_seeds_to_buckets` overwrites every
/// entry's copy with the same pair of values, and nothing adds entries after
/// it. They live on [`Buckets`] as scalars and are materialized only into
/// `get_sorted_buckets`'s output.
#[derive(Clone, Copy, Default)]
struct BucketEntry {
    loc: BucketLoc,
    matches: QPos,
    codirection: i32,
    r_min: RPos,
    r_max: RPos,
}

impl BucketEntry {
    #[inline(always)]
    fn new(loc: BucketLoc, c: &BucketContent) -> Self {
        // The two dropped fields are always these constants at push time;
        // `propagate_seeds_to_buckets` is what ever sets them to anything else.
        debug_assert!(c.i == -1 && c.seeds == 0);
        BucketEntry {
            loc,
            matches: c.matches,
            codirection: c.codirection,
            r_min: c.r_min,
            r_max: c.r_max,
        }
    }

    /// Folds another contribution to the same bucket in — the same arithmetic
    /// as `BucketContent`'s `AddAssign`, minus the two hoisted fields.
    #[inline(always)]
    fn merge(&mut self, o: &BucketEntry) {
        self.matches += o.matches;
        self.codirection += o.codirection;
        self.r_min = self.r_min.min(o.r_min);
        self.r_max = self.r_max.max(o.r_max);
    }
}

/// Bucket accumulator storage backed by a hashmap, keyed by `BucketLoc`.
///
/// Upstream, this is used only as ephemeral per-seed scratch space inside
/// `match_seeds` (to de-duplicate one seed's own multi-hits before merging
/// them into the main `Buckets` store) — it is *not* a swappable
/// alternative backend for the mapper's primary bucket storage (that's
/// always `Buckets`). Ported with only the methods that narrower role
/// actually exercises: `get_sorted_buckets`/`size` have zero call sites on
/// this type in the C++ (confirmed via grep) and are dropped, matching how
/// `Buckets::size()` — a stub that literally returns `-1` — is dropped too.
pub struct BucketsHash<const AP: bool> {
    pub halflen: QPos,
    pub buckets: FxHashMap<BucketLoc, BucketContent>,
}

impl<const AP: bool> BucketsHash<AP> {
    pub fn new(halflen: QPos) -> Self {
        BucketsHash {
            halflen,
            buckets: FxHashMap::default(),
        }
    }

    pub fn begin(&self, b: &BucketLoc) -> RPos {
        b.b * self.halflen
    }

    pub fn end(&self, b: &BucketLoc) -> RPos {
        (b.b + 2) * self.halflen
    }

    /// Adds `content` to the bucket containing `hit`, and to the preceding
    /// bucket too (buckets overlap: bucket `b` spans `[b, b+2)` half-lengths,
    /// so a hit in half-length `b` also falls inside bucket `b-1`).
    pub fn add_to_pos(&mut self, hit: &Hit, content: BucketContent) {
        let b = (if AP { hit.r } else { hit.tpos }) / self.halflen;
        *self.buckets.entry(BucketLoc::new(hit.segm_id, b)).or_default() += content;
        if b > 0 {
            *self
                .buckets
                .entry(BucketLoc::new(hit.segm_id, b - 1))
                .or_default() += content;
        }
    }

    /// Empties the map for reuse as the next seed's scratch space, keeping
    /// its already-allocated capacity — lets a single `BucketsHash` be
    /// reused across every multi-hit seed in a read (`match_seeds`) instead
    /// of allocating a fresh one per seed.
    pub fn clear(&mut self) {
        self.buckets.clear();
    }
}

/// The mapper's primary bucket storage, keyed by `BucketLoc` (segment +
/// `tpos / halflen`, or `r / halflen` when `AP`) rather than a flat,
/// reference-sized array.
///
/// This used to be one dense `Vec<BucketContent>` per reference segment,
/// sized up front from the segment's length (`sz / MIN_HALFLEN + 2` slots) —
/// for a multi-Gbp genome that's a ~15 GB allocation *per worker thread*,
/// re-zeroed on every `clear()`-tracked touch but otherwise sitting almost
/// entirely idle (a read only ever touches a handful of buckets near where
/// it maps). Profiling that one-time allocation+zero-init (see
/// `PROFILING.md`) found it costs 7-21+ seconds per worker depending on how
/// many other workers are doing the same thing concurrently — the single
/// largest hidden cost in the whole mapper, and the direct cause of
/// multithreaded whole-genome runs sometimes getting *slower* with more
/// threads (workers that finish this allocation last can end up with zero
/// reads by the time they're ready).
///
/// An intermediate version keyed this by an `FxHashMap<BucketLoc,
/// BucketContent>` instead (same idea: only touched buckets exist, so memory
/// scales with reads, not reference size). That fixed the memory blowup but
/// introduced a *speed* regression on repetitive references: k=15 seeds on a
/// whole genome touch millions of buckets per read, and every touch was a
/// full hashmap `entry()` (hash + probe + possible resize) — on that
/// workload it made single-threaded mapping ~20% *slower* than the original
/// dense array, despite the huge memory win. Replacing that with an
/// append-only `Vec` plus one batched radix sort + dedup per read recovered
/// the speed, at the cost of materializing every raw contribution.
///
/// # The dense path
///
/// Measuring that design on the whole-genome k=15 HiFi benchmark showed why
/// it was still the dominant cost: a read produced ~4M raw contributions but
/// only ~242k *distinct* buckets, and sorting 4M records to collapse them to
/// 242k moved ~1.1 GB per read at memory-bandwidth speed. The insight that
/// removes the sort entirely is that the bucket space is small and *known*:
/// there are only `reference_sketch_len / halflen` buckets in the whole
/// reference (~242k for that workload), and such a read touches nearly all
/// of them anyway. So `Buckets` accumulates straight into a dense array of
/// one 16-byte slot per bucket (~4 MB — L3-resident), indexed by a global
/// bucket id, and recovers the sorted, deduplicated result with a single
/// linear scan. There is no sort, no dedup pass, and no per-contribution
/// record: `add_to_pos`/`add_to_bucket` become one indexed read-modify-write.
///
/// This is *not* a return to the original dense array. That one was sized
/// from the reference length over `MIN_HALFLEN` (~15 GB per worker); this one
/// is sized from the reference length over the *read's own* half-length, three
/// to four orders of magnitude smaller, and it refuses to allocate at all
/// beyond [`MAX_DENSE_SLOTS`] — falling back to the sparse append + radix
/// sort path, which is retained for exactly that case.
pub struct Buckets<'idx, const AP: bool> {
    tidx: &'idx SketchIndex,
    pub halflen: QPos,
    pub i: i32,
    pub seeds: i32,
    /// Append-only per-read scratch: every `add_to_pos`/`add_to_bucket` call
    /// pushes one entry, and the same `BucketLoc` may appear many times
    /// before `merge_entries` folds duplicates together.
    entries: Vec<BucketEntry>,
    /// Ping-pong buffer for [`Self::radix_sort_entries`], reused (grown,
    /// never shrunk) across reads to avoid reallocating every call.
    radix_scratch: Vec<BucketEntry>,
    /// Reused counting-sort histograms for the radix sort: one
    /// `digit_size`-wide histogram per pass, laid out end to end. Grown on
    /// first use rather than pre-allocated at the worst case, so a worker
    /// that only ever takes the dense path never pays for it.
    radix_counts: Vec<u32>,
    /// Set once `entries` has been sorted+deduplicated, cleared by
    /// `add_to_pos`/`add_to_bucket`/`clear` — lets `merge_entries` (called
    /// once from `propagate_seeds_to_buckets` and again from
    /// `get_sorted_buckets` on every read) skip the second, redundant sort.
    merged: bool,
    /// The uniform `BucketContent::i`/`seeds` that `propagate_seeds_to_buckets`
    /// stamps onto every bucket, hoisted out of the per-entry payload.
    prop_i: i32,
    prop_seeds: QPos,
    /// Dense per-bucket accumulators for the fast path, indexed by a global
    /// bucket id (`seg_base[segm_id] + b`). Empty when the sparse path is in
    /// use for this read.
    dense: Vec<DenseSlot>,
    /// `seg_base[sid]` is the global id of segment `sid`'s bucket 0. Ordering
    /// by global id is the same as ordering by `(segm_id, b)`, which is why
    /// a single ascending scan of `dense` yields exactly the sorted,
    /// deduplicated list the sparse path's radix sort + dedup produces.
    seg_base: Vec<u32>,
    /// Whether this read is using the dense path.
    dense_on: bool,
    /// Running maxima of the pushed `b` / `segm_id`, tracked as entries
    /// arrive rather than re-derived by scanning them. The radix sort needs
    /// them only to size its key, and a scan over `entries` to find them is a
    /// full extra read of ~95 MB per read on a whole-genome k=15 run — the
    /// same cost as one of the sort passes it is trying to size.
    max_b: u32,
    max_sid: u32,
    /// Reused `(descending matches, ascending index)` sort keys for
    /// [`Self::get_sorted_buckets`].
    order: Vec<u64>,
}

impl<'idx, const AP: bool> Buckets<'idx, AP> {
    pub fn new(tidx: &'idx SketchIndex) -> Self {
        Buckets {
            tidx,
            halflen: -1,
            i: 0,
            seeds: 0,
            entries: Vec::new(),
            radix_scratch: Vec::new(),
            radix_counts: Vec::new(),
            merged: true,
            prop_i: -1,
            prop_seeds: 0,
            dense: Vec::new(),
            seg_base: Vec::new(),
            dense_on: false,
            max_b: 0,
            max_sid: 0,
            order: Vec::new(),
        }
    }

    /// Clears the per-read scratch buffer for reuse, keeping its
    /// already-allocated capacity (no reallocation across reads).
    pub fn clear(&mut self) {
        self.i = 0;
        self.seeds = 0;
        self.entries.clear();
        self.max_b = 0;
        self.max_sid = 0;
        // Reset alongside `i`/`seeds`, which they mirror: a read that somehow
        // reached `get_sorted_buckets` without `propagate_seeds_to_buckets`
        // must see the same defaults the per-entry copies used to carry.
        self.prop_i = -1;
        self.prop_seeds = 0;
        self.merged = true;
    }

    /// Sets the bucket half-length; returns `false` if it's below
    /// `MIN_HALFLEN` (the caller should treat that as "too small to map
    /// usefully" rather than a hard error, matching the C++).
    pub fn set_halflen(&mut self, new_halflen: QPos) -> bool {
        self.halflen = new_halflen;
        if self.halflen < MIN_HALFLEN {
            self.dense_on = false;
            return false;
        }
        self.plan_dense();
        true
    }

    /// Decides whether this read can use the dense accumulator and, if so,
    /// lays out `seg_base` and (re)zeroes `dense`.
    ///
    /// Called once per read, from `set_halflen`, which runs after
    /// `Buckets::clear` — so the zeroing here is also what guarantees no
    /// state leaks from a previous read even if that read never extracted.
    fn plan_dense(&mut self) {
        let tidx = self.tidx;
        let halflen = self.halflen as i64;
        self.seg_base.clear();
        let mut total: i64 = 0;
        for seg in &tidx.segments {
            self.seg_base.push(total as u32);
            // Bucket indices run `0 ..= max_pos / halflen`; `+ 2` covers that
            // inclusive end and keeps the `b - 1` neighbour in range.
            let extent = if AP { seg.sz as i64 } else { seg.kmers.len() as i64 };
            total += (extent.max(1) - 1) / halflen + 2;
            if total > MAX_DENSE_SLOTS as i64 {
                self.seg_base.clear();
                self.dense = Vec::new();
                self.dense_on = false;
                return;
            }
        }
        // `clear` + `resize` rewrites every slot, which is the per-read reset.
        self.dense.clear();
        self.dense.resize(total as usize, EMPTY_SLOT);
        self.dense_on = true;
    }

    /// Global slot id for `(segm_id, b)`.
    #[inline(always)]
    fn slot(&self, segm_id: SegmId, b: RPos) -> usize {
        // `b` is `pos / halflen` where `pos` is a hit's `tpos` (an index into
        // the segment's sketch) or its `r` (a position within the segment),
        // which is exactly what `plan_dense` sized each segment's range from.
        debug_assert!(segm_id >= 0 && b >= 0);
        self.seg_base[segm_id as usize] as usize + b as usize
    }

    /// Walks the dense accumulator in ascending global-id order, moving every
    /// touched slot into `entries` and resetting it. Because global ids are
    /// ordered by `(segm_id, b)`, this produces the same sorted, deduplicated
    /// `entries` the sparse path builds with a radix sort followed by a
    /// dedup scan — but in one linear pass over `dense` instead of several
    /// over every raw contribution.
    fn extract_dense(&mut self) {
        self.entries.clear();
        let n_seg = self.seg_base.len();
        for sid in 0..n_seg {
            let start = self.seg_base[sid] as usize;
            let end = if sid + 1 < n_seg {
                self.seg_base[sid + 1] as usize
            } else {
                self.dense.len()
            };
            for g in start..end {
                let slot = self.dense[g];
                if slot.matches > 0 {
                    self.dense[g] = EMPTY_SLOT;
                    self.entries.push(BucketEntry {
                        loc: BucketLoc::new(sid as SegmId, (g - start) as RPos),
                        matches: slot.matches,
                        codirection: slot.codirection,
                        r_min: slot.r_min,
                        r_max: slot.r_max,
                    });
                }
            }
        }
    }

    pub fn begin(&self, b: &BucketLoc) -> RPos {
        b.b * self.halflen
    }

    pub fn end(&self, b: &BucketLoc) -> RPos {
        (b.b + 2) * self.halflen
    }

    /// Sorts `self.entries` ascending by `(segm_id, b)` in O(n) via an LSD
    /// radix sort on [`radix_key`], instead of an O(n log n) comparison
    /// sort. On repetitive references (k=15 whole-genome, where a read's
    /// seeds can touch millions of raw entries — see the module doc
    /// comment), profiling found this location-sort was ~77% of total
    /// mapping time: a generic comparison sort pays a `log n` factor *and*
    /// a closure call per comparison for what's really just two bounded
    /// integers. Radix sort trades that for a handful of linear passes over
    /// contiguous memory (each pass: one counting scan, one prefix sum over
    /// the small per-pass histogram, one scatter into `radix_scratch`).
    ///
    /// The number of passes is computed from the actual data rather than
    /// fixed at [`RADIX_MAX_PASSES`]: `segm_id` is a handful of segments and
    /// `b` is bounded by `segment_len / halflen` (a read-scaled bucket
    /// half-length), so the packed key's highest set bit is usually well
    /// below 64 — skipping an always-zero high pass saves a full O(n)
    /// counting+scatter pass for one cheap sequential max-reduce.
    ///
    /// Every pass's histogram is built in a *single* counting scan rather
    /// than one scan per pass. A pass only permutes the entries, and a
    /// histogram does not care about order, so the later passes' digit counts
    /// are already knowable from the original array. On this workload that
    /// removes a whole O(n) read over ~95 MB per read.
    fn radix_sort_entries(&mut self) {
        let n = self.entries.len();
        if n <= 1 {
            return;
        }
        // How wide the key actually is, from the maxima tracked at push time.
        // `b` is a position divided by a read-scaled half-length and
        // `segm_id` counts chromosomes, so this typically lands at ~22 bits —
        // two digits instead of the four a fixed 64-bit key would need.
        let b_bits = u32::BITS - self.max_b.leading_zeros();
        let key_bits = b_bits + (u32::BITS - self.max_sid.leading_zeros());
        if key_bits == 0 {
            return;
        }
        let passes = key_bits.div_ceil(RADIX_MAX_BITS).min(RADIX_MAX_PASSES);
        // Spread the key evenly over the passes we are paying for anyway.
        let digit_bits = key_bits.div_ceil(passes).min(RADIX_MAX_BITS);
        let digit_size = 1usize << digit_bits;
        let digit_mask = (digit_size as u64) - 1;

        self.radix_scratch.resize(n, BucketEntry::default());
        let need = passes as usize * digit_size;
        if self.radix_counts.len() < need {
            self.radix_counts.resize(need, 0);
        }

        // All passes' histograms in one scan over `entries`, laid out as
        // `pass * digit_size + digit`.
        let hist = &mut self.radix_counts[..(passes as usize) * digit_size];
        hist.iter_mut().for_each(|c| *c = 0);
        for e in &self.entries {
            let key = radix_key(&e.loc, b_bits);
            for pass in 0..passes as usize {
                let d = ((key >> (pass as u32 * digit_bits)) & digit_mask) as usize;
                hist[pass * digit_size + d] += 1;
            }
        }
        // Turn each pass's counts into starting offsets in place.
        for pass in 0..passes as usize {
            let mut sum = 0u32;
            for c in hist[pass * digit_size..(pass + 1) * digit_size].iter_mut() {
                let cur = *c;
                *c = sum;
                sum += cur;
            }
        }

        for pass in 0..passes {
            let shift = pass * digit_bits;
            let offsets = &mut self.radix_counts[(pass as usize) * digit_size..][..digit_size];
            for e in &self.entries {
                let d = ((radix_key(&e.loc, b_bits) >> shift) & digit_mask) as usize;
                self.radix_scratch[offsets[d] as usize] = *e;
                offsets[d] += 1;
            }
            std::mem::swap(&mut self.entries, &mut self.radix_scratch);
        }
    }

    /// Sorts `entries` by location and folds every run of matching
    /// `BucketLoc`s into a single entry (summing their `BucketContent` via
    /// `AddAssign`, same semantics as the old per-hit hashmap merge). Safe
    /// to call more than once per read: guarded by `merged` so a repeat call
    /// (see `propagate_seeds_to_buckets`/`get_sorted_buckets`) is a no-op
    /// rather than re-sorting already-deduplicated data.
    fn merge_entries(&mut self) {
        if self.merged {
            return;
        }
        self.merged = true;
        if self.dense_on {
            self.extract_dense();
            return;
        }
        if self.entries.is_empty() {
            return;
        }
        self.radix_sort_entries();
        let mut write = 0usize;
        for read in 1..self.entries.len() {
            if self.entries[read].loc == self.entries[write].loc {
                let c = self.entries[read];
                self.entries[write].merge(&c);
            } else {
                write += 1;
                self.entries[write] = self.entries[read];
            }
        }
        self.entries.truncate(write + 1);
    }

    pub fn propagate_seeds_to_buckets(&mut self) {
        self.merge_entries();
        // Recorded once instead of written into every entry: these are the
        // same value for every bucket of the read.
        self.prop_i = self.i;
        self.prop_seeds = self.seeds;
    }

    pub fn add_to_pos(&mut self, hit: &Hit, content: BucketContent) {
        let b = (if AP { hit.r } else { hit.tpos }) / self.halflen;
        debug_assert!((hit.segm_id as usize) < self.tidx.segments_len());
        self.merged = false;
        if self.dense_on {
            let g = self.slot(hit.segm_id, b);
            self.dense[g].add(&content);
            if b > 0 {
                self.dense[g - 1].add(&content);
            }
            return;
        }
        self.max_b = self.max_b.max(b as u32);
        self.max_sid = self.max_sid.max(hit.segm_id as u32);
        self.entries
            .push(BucketEntry::new(BucketLoc::new(hit.segm_id, b), &content));
        if b > 0 {
            self.entries
                .push(BucketEntry::new(BucketLoc::new(hit.segm_id, b - 1), &content));
        }
    }

    pub fn add_to_bucket(&mut self, b: BucketLoc, content: BucketContent) {
        self.merged = false;
        if self.dense_on {
            let g = self.slot(b.segm_id, b.b);
            self.dense[g].add(&content);
            return;
        }
        self.max_b = self.max_b.max(b.b as u32);
        self.max_sid = self.max_sid.max(b.segm_id as u32);
        self.entries.push(BucketEntry::new(b, &content));
    }

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

        let entries = &self.entries;
        self.order
            .iter()
            .map(|&k| {
                let e = &entries[(k & 0xffff_ffff) as usize];
                (
                    e.loc,
                    BucketContent {
                        i,
                        seeds,
                        matches: e.matches,
                        codirection: e.codirection,
                        r_min: e.r_min,
                        r_max: e.r_max,
                    },
                )
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sketch::RefSegment;
    use crate::types::{Kmer, SegmId};

    /// A one-segment index whose sketch is as long as the segment, so that
    /// both bucket-index modes (`tpos`-based and `r`-based) stay in range for
    /// the positions these tests use — `tpos` is an index into `kmers`, so a
    /// segment carrying no k-mers cannot legitimately be hit at `tpos = 25`.
    fn tidx_with_one_segment(sz: RPos) -> SketchIndex {
        let mut tidx = SketchIndex::new();
        let kmers = vec![Kmer::new(0, 0, false); sz as usize];
        tidx.segments.push(RefSegment::new(kmers, "seg0".to_string(), sz, 0));
        tidx
    }

    fn hit(r: RPos, tpos: RPos, segm_id: SegmId) -> Hit {
        Hit::new(&Kmer::new(r, 0, false), tpos, segm_id)
    }

    #[test]
    fn begin_end_bucket_boundaries() {
        let tidx = tidx_with_one_segment(100);
        let mut b: Buckets<false> = Buckets::new(&tidx);
        b.set_halflen(10);

        let b0 = BucketLoc::new(0, 0);
        assert_eq!(b.begin(&b0), 0);
        assert_eq!(b.end(&b0), 20);

        let b1 = BucketLoc::new(0, 1);
        assert_eq!(b.begin(&b1), 10);
        assert_eq!(b.end(&b1), 30);
    }

    #[test]
    fn add_to_pos_touches_bucket_and_predecessor() {
        let tidx = tidx_with_one_segment(100);
        let mut b: Buckets<false> = Buckets::new(&tidx);
        b.set_halflen(10);

        // tpos=25 => bucket 2 (25/10=2), plus predecessor bucket 1.
        b.add_to_pos(&hit(25, 25, 0), BucketContent::new(1, 0, 1, 25, 25));

        let sorted = b.get_sorted_buckets();
        let locs: Vec<BucketLoc> = sorted.iter().map(|(loc, _)| *loc).collect();
        assert_eq!(locs.len(), 2);
        assert!(locs.contains(&BucketLoc::new(0, 1)));
        assert!(locs.contains(&BucketLoc::new(0, 2)));
        for (_, content) in &sorted {
            assert_eq!(content.matches, 1);
        }
    }

    #[test]
    fn add_to_pos_at_bucket_zero_does_not_touch_predecessor() {
        let tidx = tidx_with_one_segment(100);
        let mut b: Buckets<false> = Buckets::new(&tidx);
        b.set_halflen(10);

        b.add_to_pos(&hit(5, 5, 0), BucketContent::new(1, 0, 1, 5, 5));

        let sorted = b.get_sorted_buckets();
        assert_eq!(sorted.len(), 1);
        assert_eq!(sorted[0].0, BucketLoc::new(0, 0));
    }

    #[test]
    fn get_sorted_buckets_dedups_and_orders_by_matches_descending() {
        let tidx = tidx_with_one_segment(200);
        let mut b: Buckets<false> = Buckets::new(&tidx);
        b.set_halflen(10);

        // Two hits landing in the same bucket 5 (and predecessor 4).
        b.add_to_pos(&hit(50, 50, 0), BucketContent::new(1, 0, 1, 50, 50));
        b.add_to_pos(&hit(51, 51, 0), BucketContent::new(1, 0, 1, 51, 51));
        // One hit in a far bucket with fewer matches.
        b.add_to_pos(&hit(150, 150, 0), BucketContent::new(1, 0, 1, 150, 150));

        let sorted = b.get_sorted_buckets();
        // bucket 5 got 2 contributions (matches=2), bucket 4 also 2 (from
        // both add_to_pos calls' predecessor writes), bucket 15/14 got 1.
        assert!(sorted[0].1.matches >= sorted.last().unwrap().1.matches);
        // No duplicate BucketLoc entries.
        let mut locs: Vec<BucketLoc> = sorted.iter().map(|(loc, _)| *loc).collect();
        let before = locs.len();
        locs.sort_by(|a, b| a.segm_id.cmp(&b.segm_id).then(a.b.cmp(&b.b)));
        locs.dedup();
        assert_eq!(locs.len(), before);
    }

    #[test]
    fn clear_resets_touched_buckets() {
        let tidx = tidx_with_one_segment(100);
        let mut b: Buckets<false> = Buckets::new(&tidx);
        b.set_halflen(10);
        b.add_to_pos(&hit(25, 25, 0), BucketContent::new(1, 0, 1, 25, 25));
        assert!(!b.get_sorted_buckets().is_empty());

        b.clear();
        assert!(b.entries.is_empty());
        assert!(b.get_sorted_buckets().is_empty());
    }

    #[test]
    fn abs_pos_flag_selects_r_vs_tpos_for_bucket_index() {
        let tidx = tidx_with_one_segment(1000);
        let mut b_tpos: Buckets<false> = Buckets::new(&tidx);
        b_tpos.set_halflen(10);
        let mut b_abs: Buckets<true> = Buckets::new(&tidx);
        b_abs.set_halflen(10);

        // r=99 (would land in bucket 9), tpos=3 (would land in bucket 0).
        let h = hit(99, 3, 0);
        b_tpos.add_to_pos(&h, BucketContent::new(1, 0, 1, 99, 99));
        b_abs.add_to_pos(&h, BucketContent::new(1, 0, 1, 99, 99));

        let tpos_locs: Vec<BucketLoc> = b_tpos.get_sorted_buckets().into_iter().map(|(l, _)| l).collect();
        let abs_locs: Vec<BucketLoc> = b_abs.get_sorted_buckets().into_iter().map(|(l, _)| l).collect();
        assert!(tpos_locs.contains(&BucketLoc::new(0, 0)));
        assert!(abs_locs.contains(&BucketLoc::new(0, 9)));
    }

    /// The dense accumulator and the sparse radix-sort fallback must produce
    /// identical results — same buckets, same accumulated content, same
    /// order. The dense path handles every realistic workload, so without
    /// this the fallback (which exists only to bound memory on pathological
    /// short-read inputs) would go untested.
    #[test]
    fn dense_and_sparse_paths_agree() {
        // `AP` uses `sz` for the bucket extent, so a huge `sz` with a tiny
        // half-length pushes the slot count past `MAX_DENSE_SLOTS` and forces
        // the sparse path, without allocating a huge sketch.
        let mut sparse_idx = SketchIndex::new();
        sparse_idx
            .segments
            .push(RefSegment::new(Vec::new(), "seg0".to_string(), 1 << 30, 0));
        let dense_idx = tidx_with_one_segment(4000);

        let mut sparse: Buckets<true> = Buckets::new(&sparse_idx);
        let mut dense: Buckets<true> = Buckets::new(&dense_idx);
        assert!(sparse.set_halflen(10));
        assert!(dense.set_halflen(10));
        assert!(!sparse.dense_on, "expected the sparse fallback for a huge segment");
        assert!(dense.dense_on, "expected the dense path for a small segment");

        // A deterministic spread of repeated positions, so buckets are hit
        // many times over and the merge arithmetic actually matters.
        let mut rng: u64 = 0x9e37_79b9_7f4a_7c15;
        for _ in 0..5000 {
            rng = rng.wrapping_mul(6364136223846793005).wrapping_add(1);
            let r = ((rng >> 33) % 3900) as RPos;
            let content = BucketContent::new(1, 0, if r % 3 == 0 { 1 } else { -1 }, r, r);
            sparse.add_to_pos(&hit(r, r, 0), content);
            dense.add_to_pos(&hit(r, r, 0), content);
        }
        sparse.propagate_seeds_to_buckets();
        dense.propagate_seeds_to_buckets();

        let a = sparse.get_sorted_buckets();
        let b = dense.get_sorted_buckets();
        assert_eq!(a.len(), b.len(), "bucket count differs between paths");
        assert!(!a.is_empty());
        for (x, y) in a.iter().zip(b.iter()) {
            assert_eq!(x.0, y.0, "bucket location differs");
            assert_eq!(
                (x.1.matches, x.1.codirection, x.1.r_min, x.1.r_max),
                (y.1.matches, y.1.codirection, y.1.r_min, y.1.r_max),
                "accumulated content differs at {:?}",
                x.0
            );
        }
    }

    #[test]
    fn buckets_hash_add_to_pos_touches_bucket_and_predecessor() {
        let mut bh: BucketsHash<false> = BucketsHash::new(10);
        bh.add_to_pos(&hit(25, 25, 0), BucketContent::new(1, 0, 1, 25, 25));
        assert_eq!(bh.buckets.len(), 2);
        assert!(bh.buckets.contains_key(&BucketLoc::new(0, 1)));
        assert!(bh.buckets.contains_key(&BucketLoc::new(0, 2)));
    }
}
