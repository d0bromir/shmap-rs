//! FracMinHash k-mer sketching.
//!
//! Port of the sketching half of `shmap/src/sketch.h`.

use crate::hash::{KmerHasher, NtRollingHash};
use crate::types::{Hash, Kmer, RPos};
use crate::utils::Counters;

pub type SketchT = Vec<Kmer>;

/// A reference segment (contig/chromosome) and its k-mer sketch.
///
/// The C++ `RefSegment` also stores the segment's full nucleotide sequence
/// (`seq`), but that field is only ever read by the fully-commented-out
/// SAM/edlib alignment code — carrying it here would roughly double index
/// memory for a feature that's dead code upstream, so it's dropped.
#[derive(serde::Serialize, serde::Deserialize)]
pub struct RefSegment {
    pub kmers: SketchT,
    pub name: String,
    pub sz: RPos,
    pub id: i32,
}

impl RefSegment {
    pub fn new(kmers: SketchT, name: String, sz: RPos, id: i32) -> Self {
        RefSegment { kmers, name, sz, id }
    }
}

/// Rolling FracMinHash k-mer sketcher.
///
/// Computes a forward and reverse-complement rolling hash per k-mer window
/// via `H` (see [`KmerHasher`]; [`NtRollingHash`] — an ntHash-style
/// rotate/XOR hash — by default), and keeps only k-mers whose canonical hash
/// falls at or below the `h_frac` threshold.
pub struct FracMinHash<H: KmerHasher = NtRollingHash> {
    hasher: H,
    pub k: i32,
    pub h_frac: f64,
}

impl FracMinHash<NtRollingHash> {
    pub fn new(k: i32, h_frac: f64) -> Self {
        FracMinHash::with_hasher(k, h_frac)
    }
}

impl<H: KmerHasher> FracMinHash<H> {
    /// Builds a sketcher using a specific [`KmerHasher`] implementation,
    /// for testing/comparing alternatives to the default [`NtRollingHash`].
    pub fn with_hasher(k: i32, h_frac: f64) -> Self {
        FracMinHash {
            hasher: H::new(k),
            k,
            h_frac,
        }
    }

    /// Sketches `s` (raw ASCII nucleotide bytes), returning the k-mers
    /// passing the FracMinHash threshold, and bumps `counters`'
    /// `sketched_seqs`/`sketched_len`/`original_kmers`/`sketched_kmers`.
    pub fn sketch(&self, s: &[u8], counters: &mut Counters) -> SketchT {
        let kmers = self.sketch_into(s, Vec::new());

        counters.inc1("sketched_seqs");
        counters.inc("sketched_len", s.len() as i64);
        counters.inc("original_kmers", kmers.len() as i64);
        counters.inc("sketched_kmers", kmers.len() as i64);

        kmers
    }

    /// The selection threshold a hash must not exceed to be sketched.
    #[inline]
    fn h_thres(&self) -> Hash {
        if self.h_frac < 1.0 {
            (self.h_frac * u64::MAX as f64) as u64
        } else {
            u64::MAX
        }
    }

    /// How many k-mers `sketch_into` is expected to select from `len` bases.
    ///
    /// Selection is a Bernoulli trial per k-mer, so the count is binomial
    /// with mean `len * h_frac` and standard deviation `sqrt(mean)` (for the
    /// small `h_frac` values used in practice). Six sigma of headroom makes
    /// an overflowing push — which would double the whole `Vec` and waste far
    /// more than the slack does — vanishingly unlikely, while allocating
    /// ~0.5% over the mean on a chromosome instead of the flat 10% a fixed
    /// `1.1 *` factor costs. Overflow stays merely slow, never wrong.
    fn expected_capacity(&self, len: usize) -> usize {
        let mean = (len as f64 * self.h_frac).max(0.0);
        (mean + 6.0 * mean.sqrt()) as usize + 16
    }

    /// Sketches `s` into `buf` (cleared first), returning it. Lets a caller
    /// that sketches many sequences in a row — e.g. the per-read mapping
    /// path — reuse one allocation instead of making a fresh one per call.
    pub fn sketch_into(&self, s: &[u8], buf: SketchT) -> SketchT {
        self.sketch_slice_into(s, 0, buf)
    }

    /// Sketches `s` treating it as the sub-slice of a longer sequence that
    /// begins at `offset`, so the k-mer positions written out are positions
    /// in that longer sequence rather than in `s`.
    ///
    /// This is what makes sketching one segment splittable across threads: a
    /// window is a pure function of the `k` bases under it, so sketching
    /// `s[a..b + k - 1]` at offset `a` yields exactly the k-mers a whole-
    /// sequence sketch would have produced for the windows ending in
    /// `[a + k - 1, b + k - 2]` — bit for bit, in the same order. Concatenating
    /// consecutive slices' results therefore reconstructs the serial sketch
    /// exactly, which is what [`crate::index::SketchIndex::build_index`]
    /// relies on to stay thread-count-independent.
    pub fn sketch_slice_into(&self, s: &[u8], offset: RPos, mut buf: SketchT) -> SketchT {
        buf.clear();
        let k = self.k;
        if (s.len() as RPos) < k || k <= 0 {
            return buf;
        }
        buf.reserve(self.expected_capacity(s.len()));

        let ks = k as usize;
        let h_thres = self.h_thres();

        let (mut h_fw, mut h_rc) = self.hasher.first(&s[..ks]);

        // `r` is the right end of the window currently held in `h_fw`/`h_rc`.
        // The first window is `s[..k]`, and each iteration rolls in one base.
        //
        // Walking the incoming and outgoing bases as a pair of zipped slice
        // iterators, rather than indexing `s` twice per step by a signed
        // `RPos`, is what keeps this loop tight: the old form emitted a
        // bounds check and a sign-extension for each of `s[r]` and `s[r - k]`
        // on every base of the reference, and the mid-loop `r >= s.len()`
        // break kept LLVM from treating it as a counted loop at all.
        let mut r: RPos = k - 1 + offset;
        emit(&mut buf, r, &self.hasher, h_fw, h_rc, h_thres);
        for (&in_c, &out_c) in s[ks..].iter().zip(s.iter()) {
            (h_fw, h_rc) = self.hasher.roll(h_fw, h_rc, out_c, in_c);
            r += 1;
            emit(&mut buf, r, &self.hasher, h_fw, h_rc, h_thres);
        }

        buf
    }
}

/// Selects the k-mer ending at `r` if its canonical hash passes `h_thres`.
#[inline(always)]
fn emit<H: KmerHasher>(kmers: &mut SketchT, r: RPos, hasher: &H, h_fw: Hash, h_rc: Hash, h_thres: Hash) {
    let (h, strand) = hasher.canonical(h_fw, h_rc);
    if h <= h_thres {
        kmers.push(Kmer::new(r, h, strand));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hash::Splitmix64Hash;

    // The three properties below are checked against both `NtRollingHash`
    // (the default `FracMinHash::new` uses) and `Splitmix64Hash` (a second,
    // structurally different `KmerHasher`, plugged in via `with_hasher`) —
    // proving they hold generically, for the trait, not just for one
    // implementation's happenstance arithmetic.

    fn sketching_a_sequence_shorter_than_k_is_empty<H: KmerHasher>() {
        let sketcher = FracMinHash::<H>::with_hasher(4, 1.0);
        let mut c = Counters::new();
        assert_eq!(sketcher.sketch(b"ACC", &mut c).len(), 0);
    }

    #[test]
    fn nt_rolling_hash_sketching_a_sequence_shorter_than_k_is_empty() {
        sketching_a_sequence_shorter_than_k_is_empty::<NtRollingHash>();
    }

    #[test]
    fn splitmix64_hash_sketching_a_sequence_shorter_than_k_is_empty() {
        sketching_a_sequence_shorter_than_k_is_empty::<Splitmix64Hash>();
    }

    /// The property `build_index`'s chunked sketching depends on: splitting a
    /// sequence into overlapping slices and sketching each at its offset
    /// reproduces the whole-sequence sketch exactly, for any split points.
    fn chunked_sketching_concatenates_to_the_whole_sequence_sketch<H: KmerHasher>() {
        let k = 7;
        let sketcher = FracMinHash::<H>::with_hasher(k, 0.5);
        let mut rng: u64 = 0x1234_5678_9abc_def0;
        let seq: Vec<u8> = (0..5000)
            .map(|_| {
                rng = rng.wrapping_mul(6364136223846793005).wrapping_add(1);
                b"ACGT"[(rng >> 60) as usize % 4]
            })
            .collect();

        let whole = sketcher.sketch(&seq, &mut Counters::new());

        // `n_windows` windows exist; every way of cutting them into chunks
        // must rebuild the same sketch.
        let n_windows = seq.len() - k as usize + 1;
        for chunk in [1usize, 2, 13, 500, n_windows - 1, n_windows] {
            let mut joined: SketchT = Vec::new();
            let mut w0 = 0usize;
            while w0 < n_windows {
                let w1 = (w0 + chunk).min(n_windows);
                let slice = &seq[w0..w1 + k as usize - 1];
                joined.extend(sketcher.sketch_slice_into(slice, w0 as RPos, Vec::new()));
                w0 = w1;
            }
            assert_eq!(joined.len(), whole.len(), "chunk size {chunk} changed the k-mer count");
            for (a, b) in joined.iter().zip(whole.iter()) {
                assert_eq!(
                    (a.r, a.h, a.strand),
                    (b.r, b.h, b.strand),
                    "chunk size {chunk} diverged"
                );
            }
        }
    }

    #[test]
    fn nt_rolling_hash_chunked_sketching_concatenates_to_the_whole_sequence_sketch() {
        chunked_sketching_concatenates_to_the_whole_sequence_sketch::<NtRollingHash>();
    }

    #[test]
    fn splitmix64_hash_chunked_sketching_concatenates_to_the_whole_sequence_sketch() {
        chunked_sketching_concatenates_to_the_whole_sequence_sketch::<Splitmix64Hash>();
    }

    fn sketching_is_symmetric_under_reverse_complement<H: KmerHasher>() {
        let k = 4;
        let sketcher = FracMinHash::<H>::with_hasher(k, 1.0);
        let mut c = Counters::new();

        let s = b"ACGGT";
        let s_rc = b"ACCGT";
        let sk_s = sketcher.sketch(s, &mut c);
        let mut sk_s_rc = sketcher.sketch(s_rc, &mut c);
        sk_s_rc.reverse();

        assert_eq!(
            sk_s.len(),
            sk_s_rc.len(),
            "reverse-complement sketches should have the same size"
        );
        for i in 0..sk_s.len() {
            assert_eq!(sk_s[i].r, (i as RPos) + k - 1);
            if i < sk_s_rc.len() {
                assert_eq!(sk_s[i].r, sk_s.len() as RPos - sk_s_rc[i].r + k + 1);
                assert_eq!(sk_s[i].h, sk_s_rc[i].h);
            }
        }
    }

    #[test]
    fn nt_rolling_hash_sketching_is_symmetric_under_reverse_complement() {
        sketching_is_symmetric_under_reverse_complement::<NtRollingHash>();
    }

    #[test]
    fn splitmix64_hash_sketching_is_symmetric_under_reverse_complement() {
        sketching_is_symmetric_under_reverse_complement::<Splitmix64Hash>();
    }
}
