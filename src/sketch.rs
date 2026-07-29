//! FracMinHash k-mer sketching.
//!
//! Port of the sketching half of `shmap/src/sketch.h`.

use crate::types::{Hash, Kmer, RPos};
use crate::utils::Counters;

pub type SketchT = Vec<Kmer>;

/// A reference segment (contig/chromosome) and its k-mer sketch.
///
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

impl RefSegment {
    pub fn new(kmers: SketchT, name: String, sz: RPos, id: i32) -> Self {
        RefSegment { kmers, name, sz, id }
    }
}

/// Rolling FracMinHash k-mer sketcher.
///
/// Builds a forward and reverse-complement rolling hash per k-mer window
/// using two 256-entry lookup tables, and keeps only k-mers whose
/// (canonical) hash falls at or below the `h_frac` threshold.
pub struct FracMinHash {
    lut_fw: [Hash; 256],
    lut_rc: [Hash; 256],
    /// Per-base contributions with the fixed rotates the rolling update
    /// applies to the *outgoing*/*incoming* base baked in, so the hot loop
    /// does a plain table load instead of a load+rotate each. Since these
    /// rotate amounts (`k`, `1`, `k-1`) are the same for every base, this
    /// removes 3 of the 5 per-base rotates over the whole reference — see
    /// [`FracMinHash::sketch_into`]. `lut_fw_k[c] = lut_fw[c].rotate_left(k)`,
    /// `lut_rc_r1[c] = lut_rc[c].rotate_right(1)`,
    /// `lut_rc_k1[c] = lut_rc[c].rotate_left(k-1)`.
    ///
    /// Interleaving each base's forward/reverse pair into one `[Hash; 2]`
    /// table, to halve the per-base load count, was tried and measured
    /// ~6% *slower* — the 16-byte load goes through a vector register and
    /// has to be split again before the scalar xors.
    lut_fw_k: [Hash; 256],
    lut_rc_r1: [Hash; 256],
    lut_rc_k1: [Hash; 256],
    pub k: i32,
    pub h_frac: f64,
}

impl FracMinHash {
    pub fn new(k: i32, h_frac: f64) -> Self {
        // https://gist.github.com/Daniel-Liu-c0deb0t/7078ebca04569068f15507aa856be6e8
        const A: Hash = 0x3c8b_fbb3_95c6_0474;
        const C: Hash = 0x3193_c185_62a0_2b4c;
        const G: Hash = 0x2032_3ed0_8257_2324;
        const TN: Hash = 0x2955_49f5_4be2_4456;

        // The C++ leaves every other LUT entry as uninitialized stack
        // memory (`hash_t LUT_fw[256]` is a raw array member, never
        // value-initialized before `initialize_LUT()` fills in exactly 8
        // slots) — reading it for any non-ACGT byte (N, ambiguity codes,
        // ...) is undefined behavior there. Zero-initializing here instead
        // makes unknown bases deterministically contribute a hash of 0,
        // which is well-defined and doesn't change behavior for any ACGT
        // (or ACGT-only test) input.
        let mut lut_fw = [0u64; 256];
        let mut lut_rc = [0u64; 256];

        for &(lower, upper, v) in &[(b'a', b'A', A), (b'c', b'C', C), (b'g', b'G', G), (b't', b'T', TN)] {
            lut_fw[lower as usize] = v;
            lut_fw[upper as usize] = v;
        }
        for &(lower, upper, complement) in &[
            (b'a', b'A', b'T'),
            (b'c', b'C', b'G'),
            (b'g', b'G', b'C'),
            (b't', b'T', b'A'),
        ] {
            lut_rc[lower as usize] = lut_fw[complement as usize];
            lut_rc[upper as usize] = lut_fw[complement as usize];
        }

        let mut lut_fw_k = [0u64; 256];
        let mut lut_rc_r1 = [0u64; 256];
        let mut lut_rc_k1 = [0u64; 256];
        for c in 0..256 {
            lut_fw_k[c] = lut_fw[c].rotate_left(k as u32);
            lut_rc_r1[c] = lut_rc[c].rotate_right(1);
            lut_rc_k1[c] = lut_rc[c].rotate_left((k - 1) as u32);
        }

        FracMinHash {
            lut_fw,
            lut_rc,
            lut_fw_k,
            lut_rc_r1,
            lut_rc_k1,
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

        let mut h_fw: Hash = 0;
        let mut h_rc: Hash = 0;
        for (i, &c) in s[..ks].iter().enumerate() {
            let c = c as usize;
            h_fw ^= self.lut_fw[c].rotate_left((ks - i - 1) as u32);
            h_rc ^= self.lut_rc[c].rotate_left(i as u32);
        }

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
        emit(&mut buf, r, h_fw, h_rc, h_thres);
        for (&in_c, &out_c) in s[ks..].iter().zip(s.iter()) {
            // Identical arithmetic to the pre-baked form (see the LUT doc
            // comment) — the three fixed rotates on LUT values are now
            // precomputed, leaving only the two accumulator rotates here.
            let (in_c, out_c) = (in_c as usize, out_c as usize);
            h_fw = h_fw.rotate_left(1) ^ self.lut_fw_k[out_c] ^ self.lut_fw[in_c];
            h_rc = h_rc.rotate_right(1) ^ self.lut_rc_r1[out_c] ^ self.lut_rc_k1[in_c];
            r += 1;
            emit(&mut buf, r, h_fw, h_rc, h_thres);
        }

        buf
    }
}

/// Selects the k-mer ending at `r` if its canonical hash passes `h_thres`.
#[inline(always)]
fn emit(kmers: &mut SketchT, r: RPos, h_fw: Hash, h_rc: Hash, h_thres: Hash) {
    let h = h_rc ^ h_fw;
    if h <= h_thres {
        kmers.push(Kmer::new(r, h, h_fw > h_rc));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sketching_a_sequence_shorter_than_k_is_empty() {
        let sketcher = FracMinHash::new(4, 1.0);
        let mut c = Counters::new();
        assert_eq!(sketcher.sketch(b"ACC", &mut c).len(), 0);
    }

    /// The property `build_index`'s chunked sketching depends on: splitting a
    /// sequence into overlapping slices and sketching each at its offset
    /// reproduces the whole-sequence sketch exactly, for any split points.
    #[test]
    fn chunked_sketching_concatenates_to_the_whole_sequence_sketch() {
        let k = 7;
        let sketcher = FracMinHash::new(k, 0.5);
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
    fn sketching_is_symmetric_under_reverse_complement() {
        let k = 4;
        let sketcher = FracMinHash::new(k, 1.0);
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
}
