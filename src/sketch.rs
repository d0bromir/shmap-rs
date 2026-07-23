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
    /// [`FracMinHash::sketch`]. `lut_fw_k[c] = lut_fw[c].rotate_left(k)`,
    /// `lut_rc_r1[c] = lut_rc[c].rotate_right(1)`,
    /// `lut_rc_k1[c] = lut_rc[c].rotate_left(k-1)`.
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
        for &(lower, upper, complement) in &[(b'a', b'A', b'T'), (b'c', b'C', b'G'), (b'g', b'G', b'C'), (b't', b'T', b'A')] {
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
        let k = self.k;
        let mut kmers: SketchT =
            Vec::with_capacity((1.1 * s.len() as f64 * self.h_frac).max(0.0) as usize);

        if (s.len() as RPos) < k {
            return kmers;
        }

        let h_thres: Hash = if self.h_frac < 1.0 {
            (self.h_frac * u64::MAX as f64) as u64
        } else {
            u64::MAX
        };

        let mut h_fw: Hash = 0;
        let mut h_rc: Hash = 0;
        let mut r: RPos = 0;

        while r < k {
            let c = s[r as usize] as usize;
            h_fw ^= self.lut_fw[c].rotate_left((k - r - 1) as u32);
            h_rc ^= self.lut_rc[c].rotate_left(r as u32);
            r += 1;
        }

        loop {
            let h = h_rc ^ h_fw;
            if h <= h_thres {
                let strand = h_fw > h_rc;
                kmers.push(Kmer::new(r - 1, h, strand));
            }

            if r >= s.len() as RPos {
                break;
            }

            let out_c = s[(r - k) as usize] as usize;
            let in_c = s[r as usize] as usize;
            // Identical arithmetic to the pre-baked form (see the LUT doc
            // comment) — the three fixed rotates on LUT values are now
            // precomputed, leaving only the two accumulator rotates here.
            h_fw = h_fw.rotate_left(1) ^ self.lut_fw_k[out_c] ^ self.lut_fw[in_c];
            h_rc = h_rc.rotate_right(1) ^ self.lut_rc_r1[out_c] ^ self.lut_rc_k1[in_c];

            r += 1;
        }

        counters.inc1("sketched_seqs");
        counters.inc("sketched_len", s.len() as i64);
        counters.inc("original_kmers", kmers.len() as i64);
        counters.inc("sketched_kmers", kmers.len() as i64);

        kmers
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

        assert_eq!(sk_s.len(), sk_s_rc.len(), "reverse-complement sketches should have the same size");
        for i in 0..sk_s.len() {
            assert_eq!(sk_s[i].r, (i as RPos) + k - 1);
            if i < sk_s_rc.len() {
                assert_eq!(sk_s[i].r, sk_s.len() as RPos - sk_s_rc[i].r + k + 1);
                assert_eq!(sk_s[i].h, sk_s_rc[i].h);
            }
        }
    }
}
