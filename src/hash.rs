//! Pluggable k-mer hash functions.
//!
//! [`FracMinHash`](crate::sketch::FracMinHash) needs a forward and a
//! reverse-complement hash per k-mer window, updated one base at a time as
//! the window slides across a sequence (see
//! [`FracMinHash::sketch_slice_into`](crate::sketch::FracMinHash::sketch_slice_into)) —
//! rehashing the whole k-mer at every position would cost `O(k)` per base
//! instead of `O(1)`. [`KmerHasher`] captures that rolling-update shape so
//! alternative hash functions can be swapped in (for testing, or comparing
//! effects on mapping accuracy/speed) without touching the sketching or
//! selection logic in `sketch.rs`.
//!
//! [`NtRollingHash`] is the hash `shmap` has always used — an ntHash-style
//! rotate/XOR rolling hash — and is `FracMinHash`'s default. [`Splitmix64Hash`]
//! is a second, structurally different implementation (rolls a packed 2-bit
//! k-mer, hashes it with the splitmix64 finalizer) that exists to prove the
//! trait is actually pluggable and to give something to benchmark/compare
//! against; it is not used by default anywhere.

use crate::types::Hash;

/// A hash function applied to a k-mer and its reverse complement, updated
/// incrementally as a window of length `k` slides one base at a time.
///
/// The `Hash` values threaded through `first`/`roll` are whatever an
/// implementor needs to carry between windows to update in O(1) per base —
/// for [`NtRollingHash`] that's the hash itself; for a hasher built around a
/// one-shot mixing function (like [`Splitmix64Hash`]) it can instead be an
/// intermediate representation (e.g. a packed k-mer) that only gets
/// finalized into the emitted hash by [`KmerHasher::canonical`].
pub trait KmerHasher: Sized {
    /// Builds a hasher for k-mers of length `k`.
    fn new(k: i32) -> Self;

    /// The forward/reverse-complement rolling state for the first window,
    /// `window` (of length `k`).
    fn first(&self, window: &[u8]) -> (Hash, Hash);

    /// Rolls the window forward by one base: `out_c` is the base leaving the
    /// window (its leftmost base), `in_c` is the base entering it.
    fn roll(&self, h_fw: Hash, h_rc: Hash, out_c: u8, in_c: u8) -> (Hash, Hash);

    /// Combines a window's forward/reverse-complement rolling state into the
    /// canonical hash that gets selected on, and the strand it came from
    /// (`true` = the reverse-complement hash was smaller).
    #[inline]
    fn canonical(&self, h_fw: Hash, h_rc: Hash) -> (Hash, bool) {
        (h_fw ^ h_rc, h_fw > h_rc)
    }
}

/// The ntHash-style rotate/XOR rolling hash `shmap` has always used.
///
/// Builds a forward and reverse-complement rolling hash per k-mer window
/// using two 256-entry lookup tables, keyed by raw ASCII byte.
pub struct NtRollingHash {
    lut_fw: [Hash; 256],
    lut_rc: [Hash; 256],
    /// Per-base contributions with the fixed rotates the rolling update
    /// applies to the *outgoing*/*incoming* base baked in, so the hot loop
    /// does a plain table load instead of a load+rotate each. Since these
    /// rotate amounts (`k`, `1`, `k-1`) are the same for every base, this
    /// removes 3 of the 5 per-base rotates over the whole reference — see
    /// [`NtRollingHash::roll`]. `lut_fw_k[c] = lut_fw[c].rotate_left(k)`,
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
}

impl KmerHasher for NtRollingHash {
    fn new(k: i32) -> Self {
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

        NtRollingHash {
            lut_fw,
            lut_rc,
            lut_fw_k,
            lut_rc_r1,
            lut_rc_k1,
        }
    }

    #[inline]
    fn first(&self, window: &[u8]) -> (Hash, Hash) {
        let ks = window.len();
        let mut h_fw: Hash = 0;
        let mut h_rc: Hash = 0;
        for (i, &c) in window.iter().enumerate() {
            let c = c as usize;
            h_fw ^= self.lut_fw[c].rotate_left((ks - i - 1) as u32);
            h_rc ^= self.lut_rc[c].rotate_left(i as u32);
        }
        (h_fw, h_rc)
    }

    #[inline]
    fn roll(&self, h_fw: Hash, h_rc: Hash, out_c: u8, in_c: u8) -> (Hash, Hash) {
        // Identical arithmetic to the pre-baked form (see the LUT doc
        // comment) — the three fixed rotates on LUT values are precomputed,
        // leaving only the two accumulator rotates here.
        let (in_c, out_c) = (in_c as usize, out_c as usize);
        let h_fw = h_fw.rotate_left(1) ^ self.lut_fw_k[out_c] ^ self.lut_fw[in_c];
        let h_rc = h_rc.rotate_right(1) ^ self.lut_rc_r1[out_c] ^ self.lut_rc_k1[in_c];
        (h_fw, h_rc)
    }
}

/// A structurally different rolling hash, built to prove [`KmerHasher`] is
/// actually pluggable and to give something to compare [`NtRollingHash`]
/// against — not used anywhere by default.
///
/// Rolls a 2-bit-per-base packed k-mer (forward, and separately its reverse
/// complement) through a plain shift register, then finalizes each with the
/// splitmix64 mixer. Packing needs `2*k` bits to fit a `u64`, so this only
/// supports `k <= 32`; unrecognized bytes (anything but `ACGTacgt`) pack as
/// `A`, unlike `NtRollingHash`'s "contributes zero" handling — fine for a
/// comparison/test hasher, not a general replacement.
pub struct Splitmix64Hash {
    mask: Hash,
    /// `2*(k-1)`: the bit offset of the highest base slot in the packed
    /// k-mer, i.e. where an incoming base lands in the reverse-complement
    /// packing (see [`Splitmix64Hash::roll`]).
    rc_shift: u32,
}

impl Splitmix64Hash {
    #[inline]
    fn code(c: u8) -> Hash {
        match c {
            b'A' | b'a' => 0,
            b'C' | b'c' => 1,
            b'G' | b'g' => 2,
            b'T' | b't' => 3,
            _ => 0,
        }
    }

    #[inline]
    fn mix(mut x: Hash) -> Hash {
        x ^= x >> 30;
        x = x.wrapping_mul(0xbf58_476d_1ce4_e5b9);
        x ^= x >> 27;
        x = x.wrapping_mul(0x94d0_49bb_1331_11eb);
        x ^= x >> 31;
        x
    }
}

impl KmerHasher for Splitmix64Hash {
    fn new(k: i32) -> Self {
        assert!(
            (1..=32).contains(&k),
            "Splitmix64Hash packs 2 bits/base into a u64, so it only supports 1 <= k <= 32 (got k={k})"
        );
        Splitmix64Hash {
            mask: if k == 32 { Hash::MAX } else { (1u64 << (2 * k)) - 1 },
            rc_shift: 2 * (k as u32 - 1),
        }
    }

    #[inline]
    fn first(&self, window: &[u8]) -> (Hash, Hash) {
        let mut packed_fw: Hash = 0;
        let mut packed_rc: Hash = 0;
        for (i, &c) in window.iter().enumerate() {
            let code = Self::code(c);
            packed_fw = (packed_fw << 2) | code;
            // 3 - code: A(0)<->T(3), C(1)<->G(2).
            packed_rc |= (3 - code) << (2 * i as u32);
        }
        (packed_fw, packed_rc)
    }

    #[inline]
    fn roll(&self, h_fw: Hash, h_rc: Hash, _out_c: u8, in_c: u8) -> (Hash, Hash) {
        let code = Self::code(in_c);
        let packed_fw = ((h_fw << 2) | code) & self.mask;
        let packed_rc = (h_rc >> 2) | ((3 - code) << self.rc_shift);
        (packed_fw, packed_rc)
    }

    #[inline]
    fn canonical(&self, h_fw: Hash, h_rc: Hash) -> (Hash, bool) {
        let (m_fw, m_rc) = (Self::mix(h_fw), Self::mix(h_rc));
        (m_fw ^ m_rc, m_fw > m_rc)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Rolling k bases forward one at a time must land on the same state as
    /// hashing that window from scratch — for both hashers.
    fn rolling_matches_first_from_scratch<H: KmerHasher>() {
        let k = 9;
        let hasher = H::new(k);
        let mut rng: u64 = 0x0ddc_0ffe_e0dd_1234;
        let seq: Vec<u8> = (0..500)
            .map(|_| {
                rng = rng.wrapping_mul(6364136223846793005).wrapping_add(1);
                b"ACGT"[(rng >> 60) as usize % 4]
            })
            .collect();

        let (mut h_fw, mut h_rc) = hasher.first(&seq[..k as usize]);
        for w in 1..=(seq.len() - k as usize) {
            let (out_c, in_c) = (seq[w - 1], seq[w + k as usize - 1]);
            (h_fw, h_rc) = hasher.roll(h_fw, h_rc, out_c, in_c);
            let (expected_fw, expected_rc) = hasher.first(&seq[w..w + k as usize]);
            assert_eq!((h_fw, h_rc), (expected_fw, expected_rc), "window {w} diverged");
        }
    }

    #[test]
    fn nt_rolling_hash_matches_from_scratch_hashing() {
        rolling_matches_first_from_scratch::<NtRollingHash>();
    }

    #[test]
    fn splitmix64_hash_matches_from_scratch_hashing() {
        rolling_matches_first_from_scratch::<Splitmix64Hash>();
    }

    /// The reverse-complement packing must be the same value as forward-
    /// packing the sequence's actual reverse complement — the property
    /// `FracMinHash`'s RC-symmetric sketching depends on.
    #[test]
    fn splitmix64_hash_rc_packing_matches_forward_packing_of_revcomp() {
        let hasher = Splitmix64Hash::new(6);
        let window = b"ACGGTA";
        let revcomp: Vec<u8> = window
            .iter()
            .rev()
            .map(|&c| match c {
                b'A' => b'T',
                b'C' => b'G',
                b'G' => b'C',
                b'T' => b'A',
                _ => unreachable!(),
            })
            .collect();

        let (_, packed_rc) = hasher.first(window);
        let (packed_fw_of_revcomp, _) = hasher.first(&revcomp);
        assert_eq!(packed_rc, packed_fw_of_revcomp);
    }
}
