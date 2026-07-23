//! Core value types shared across the mapper: k-mers, hits, seeds, matches,
//! bucket coordinates/content, and the mapping-optimization metric.
//!
//! Port of `shmap/src/types.h`.

use std::fmt;
use std::ops::AddAssign;

use rustc_hash::FxHashMap;

pub type Hash = u64;
/// Reference position. klib-derived code upstream doesn't support 64-bit
/// positions, so this stays 32-bit here too.
pub type RPos = i32;
/// Query position.
pub type QPos = i32;
pub type SegmId = i32;

/// A k-mer with metadata: a position in the sequence, its hash, and strand.
///
/// `r` is the right end of the k-mer's half-open interval `[l, r)` where
/// `l + k == r`. `strand`: `false` = forward, `true` = reverse.
#[derive(Clone, Copy, Debug)]
pub struct Kmer {
    pub r: RPos,
    pub h: Hash,
    pub strand: bool,
}

impl Kmer {
    pub fn new(r: RPos, h: Hash, strand: bool) -> Self {
        Kmer { r, h, strand }
    }
}

impl fmt::Display for Kmer {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Kmer(r={}, h={}, strand={})", self.r, self.h, self.strand)
    }
}

/// A k-mer hit in the reference T.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Hit {
    /// Right end of the k-mer `[l, r)`.
    pub r: RPos,
    /// Position in the reference sketch (index into `RefSegment::kmers`).
    pub tpos: RPos,
    pub strand: bool,
    pub segm_id: SegmId,
}

impl Hit {
    pub fn new(kmer: &Kmer, tpos: RPos, segm_id: SegmId) -> Self {
        Hit {
            r: kmer.r,
            tpos,
            strand: kmer.strand,
            segm_id,
        }
    }
}

impl fmt::Display for Hit {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "Hit(r={}, tpos={}, strand={}, segm_id={})",
            self.r, self.tpos, self.strand, self.segm_id
        )
    }
}

/// A k-mer from the query `p`, with metadata: number of hits in the
/// reference `T`, number of occurrences in `p`, and all matching positions
/// in `p` (sorted in decreasing order).
#[derive(Clone, Debug)]
pub struct Seed {
    pub kmer: Kmer,
    pub hits_in_t: RPos,
    pub occs_in_p: QPos,
    pub seed_num: QPos,
    /// Positions in `p` of all occurrences of `kmer`; sorted decreasing.
    pub pmatches: Vec<QPos>,
}

impl Seed {
    pub fn new(
        kmer: Kmer,
        hits_in_t: RPos,
        occs_in_p: QPos,
        seed_num: QPos,
        pmatches: Vec<QPos>,
    ) -> Self {
        Seed {
            kmer,
            hits_in_t,
            occs_in_p,
            seed_num,
            pmatches,
        }
    }
}

impl fmt::Display for Seed {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "Seed({}, hits_in_T={}, occs_in_p={}, seed_num={})",
            self.kmer, self.hits_in_t, self.occs_in_p, self.seed_num
        )
    }
}

/// A pair of a seed and one of its hits.
///
/// Borrows `seed` rather than owning it (C++'s `Match` copies the whole
/// `Seed`, heap-allocated `pmatches` included, on every construction — of
/// which there can be thousands per read). Every `Vec<Match>` in this port
/// is built, consumed, and dropped within a single `map_read()` call,
/// borrowing from a `H2Seed` map that's local to that same call, so the
/// lifetime is cheap to satisfy.
#[derive(Clone, Copy, Debug)]
pub struct Match<'p> {
    pub seed: &'p Seed,
    pub hit: Hit,
}

impl<'p> Match<'p> {
    pub fn new(seed: &'p Seed, hit: Hit) -> Self {
        Match { seed, hit }
    }

    /// `+1` if the seed and hit are on the same strand, `-1` otherwise.
    pub fn codirection(&self) -> i32 {
        codirection_kmer_hit(&self.seed.kmer, &self.hit)
    }
}

/// `+1` if `kmer1`/`kmer2` are on the same strand, `-1` otherwise.
pub fn codirection_kmer_kmer(kmer1: &Kmer, kmer2: &Kmer) -> i32 {
    if kmer1.strand == kmer2.strand { 1 } else { -1 }
}

/// `+1` if `kmer`/`hit` are on the same strand, `-1` otherwise.
///
/// Ported for parity with `types.h`, though upstream never actually calls
/// this overload (only the kmer/kmer one is used, in `bestFixedLength`).
pub fn codirection_kmer_hit(kmer: &Kmer, hit: &Hit) -> i32 {
    if kmer.strand == hit.strand { 1 } else { -1 }
}

/// A bucket's coordinates: `b` refers to the half-open interval
/// `[lmax*b, lmax*(b+2))` in `tidx.T[segm_id].kmers`.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub struct BucketLoc {
    pub segm_id: SegmId,
    pub b: RPos,
}

impl BucketLoc {
    pub fn new(segm_id: SegmId, b: RPos) -> Self {
        BucketLoc { segm_id, b }
    }
}

impl Default for BucketLoc {
    /// `(-1, -1)`, matching C++'s default-constructed `BucketLoc()`. Kept
    /// as a real sentinel (rather than `Option<BucketLoc>`) because it's
    /// part of the PAF tag output contract: a mapping with no second-best
    /// candidate genuinely prints `b2:s:(-1,-1)`, which existing downstream
    /// tooling may already depend on — this isn't just an internal
    /// "uninitialized" flag the way e.g. `ParsedQueryId`'s old `valid` bool
    /// was.
    fn default() -> Self {
        BucketLoc { segm_id: -1, b: -1 }
    }
}

impl fmt::Display for BucketLoc {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "({},{})", self.segm_id, self.b)
    }
}

/// Running accumulator for a single bucket: matches/seeds seen so far,
/// net codirection, and the covered reference-position range.
#[derive(Clone, Copy, Debug)]
pub struct BucketContent {
    /// Index of the next seed (from `p_unique`) to extend this bucket with.
    pub i: i32,
    pub seeds: QPos,
    pub matches: QPos,
    pub codirection: i32,
    pub r_min: RPos,
    pub r_max: RPos,
}

impl Default for BucketContent {
    fn default() -> Self {
        BucketContent {
            i: -1,
            seeds: 0,
            matches: 0,
            codirection: 0,
            r_min: RPos::MAX,
            r_max: -1,
        }
    }
}

impl BucketContent {
    pub fn new(matches: QPos, seeds: QPos, codirection: i32, r_min: RPos, r_max: RPos) -> Self {
        BucketContent {
            i: -1,
            seeds,
            matches,
            codirection,
            r_min,
            r_max,
        }
    }
}

impl AddAssign for BucketContent {
    fn add_assign(&mut self, other: Self) {
        self.matches += other.matches;
        self.seeds += other.seeds;
        self.codirection += other.codirection;
        self.r_min = self.r_min.min(other.r_min);
        self.r_max = self.r_max.max(other.r_max);
    }
}

impl fmt::Display for BucketContent {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "(i={}, seeds={}, matches={}, codirection={}, r=[{},{}])",
            self.i, self.seeds, self.matches, self.codirection, self.r_min, self.r_max
        )
    }
}

pub type Seeds = Vec<Seed>;
pub type Matches<'p> = Vec<Match<'p>>;
pub type H2Cnt = FxHashMap<Hash, QPos>;
pub type H2Seed = FxHashMap<Hash, Seed>;

/// The mapping-optimization metric used to score/refine a candidate bucket.
#[derive(Clone, Copy, PartialEq, Eq, Debug, clap::ValueEnum)]
pub enum Metric {
    #[value(name = "Containment")]
    Containment,
    #[value(name = "Jaccard")]
    Jaccard,
    #[value(name = "bucket_SH")]
    BucketSh,
    #[value(name = "bucket_LCS")]
    BucketLcs,
}

impl Default for Metric {
    fn default() -> Self {
        Metric::Containment
    }
}

impl fmt::Display for Metric {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let s = match self {
            Metric::Containment => "Containment",
            Metric::Jaccard => "Jaccard",
            Metric::BucketSh => "bucket_SH",
            Metric::BucketLcs => "bucket_LCS",
        };
        write!(f, "{s}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bucket_content_add_assign_merges_ranges_and_sums() {
        let mut a = BucketContent::new(2, 1, 1, 10, 20);
        let b = BucketContent::new(3, 2, -1, 5, 15);
        a += b;
        assert_eq!(a.matches, 5);
        assert_eq!(a.seeds, 3);
        assert_eq!(a.codirection, 0);
        assert_eq!(a.r_min, 5);
        assert_eq!(a.r_max, 20);
    }

    #[test]
    fn codirection_matches_strand_equality() {
        let a = Kmer::new(0, 1, false);
        let b = Kmer::new(0, 2, false);
        let c = Kmer::new(0, 3, true);
        assert_eq!(codirection_kmer_kmer(&a, &b), 1);
        assert_eq!(codirection_kmer_kmer(&a, &c), -1);
    }

    #[test]
    fn metric_display_matches_cli_strings() {
        assert_eq!(Metric::Containment.to_string(), "Containment");
        assert_eq!(Metric::Jaccard.to_string(), "Jaccard");
        assert_eq!(Metric::BucketSh.to_string(), "bucket_SH");
        assert_eq!(Metric::BucketLcs.to_string(), "bucket_LCS");
    }
}
