use std::simd::Simd;
use std::simd::prelude::*;

use super::{A, C, G, TN};
use crate::types::Hash;

/// Processing lanes
const LANES: usize = 4;

const fn precompute_rotations_dec(value: Hash) -> [Hash; 128] {
    // [rot63, rot62, ..., rot0, rot63, rot62, ..., rot0]
    let mut table = [0u64; 128];
    let mut i = 0;
    while i < 128 {
        table[i] = value.rotate_left((63 - i % 64) as u32);
        i += 1;
    }
    table
}

const fn precompute_rotations_inc(value: Hash) -> [Hash; 128] {
    // [rot0, rot1, ..., rot63, rot0, rot1, ..., rot63]
    let mut table = [0u64; 128];
    let mut i = 0;
    while i < 128 {
        table[i] = value.rotate_left(i as u32);
        i += 1;
    }
    table
}

const FW_A: [Hash; 128] = precompute_rotations_dec(A);
const FW_C: [Hash; 128] = precompute_rotations_dec(C);
const FW_G: [Hash; 128] = precompute_rotations_dec(G);
const FW_T: [Hash; 128] = precompute_rotations_dec(TN);

const RC_A: [Hash; 128] = precompute_rotations_inc(TN);
const RC_C: [Hash; 128] = precompute_rotations_inc(G);
const RC_G: [Hash; 128] = precompute_rotations_inc(C);
const RC_T: [Hash; 128] = precompute_rotations_inc(A);

#[inline]
fn process(v: Simd<u8, LANES>, fw_n: usize, rc_n: usize) -> (Simd<u64, LANES>, Simd<u64, LANES>) {
    // 1. Load constants
    let v = v | Simd::splat(0x20);
    let is_a: Mask<i64, LANES> = v.simd_eq(Simd::splat(b'a')).cast();
    let is_c: Mask<i64, LANES> = v.simd_eq(Simd::splat(b'c')).cast();
    let is_g: Mask<i64, LANES> = v.simd_eq(Simd::splat(b'g')).cast();
    let is_t: Mask<i64, LANES> = v.simd_eq(Simd::splat(b't')).cast();

    // Forward: lane i reads from offset (63 - (fw_base - i)) = 63 - fw_base + i.
    // Since load_select reads [offset, offset+1, offset+2, offset+3]
    // starting at position i, we need the slice to start at (63 - fw_base).
    let fw_off = (63usize.wrapping_sub(fw_n)) & 63;
    let mut fw = Simd::splat(0);
    fw = Simd::load_select(&FW_A[fw_off..], is_a, fw);
    fw = Simd::load_select(&FW_C[fw_off..], is_c, fw);
    fw = Simd::load_select(&FW_G[fw_off..], is_g, fw);
    fw = Simd::load_select(&FW_T[fw_off..], is_t, fw);

    // Reverse complement: lane i reads from (rc_base + i)
    let rc_off = rc_n & 63;
    let mut rc = Simd::splat(0);
    rc = Simd::load_select(&RC_A[rc_off..], is_a, rc);
    rc = Simd::load_select(&RC_C[rc_off..], is_c, rc);
    rc = Simd::load_select(&RC_G[rc_off..], is_g, rc);
    rc = Simd::load_select(&RC_T[rc_off..], is_t, rc);

    (fw, rc)
}

#[inline]
pub fn hash_window(s: &[u8]) -> (Hash, Hash) {
    let ks = s.len();

    let mut fw_acc: Simd<u64, LANES> = Simd::splat(0);
    let mut rc_acc: Simd<u64, LANES> = Simd::splat(0);

    let mut fw_n = ks - 1;
    let mut rc_n = 0usize;

    let (chunks, remainder) = s.as_chunks::<LANES>();

    for chunk in chunks {
        let v: Simd<u8, LANES> = Simd::from_slice(chunk);
        let (fw, rc) = process(v, fw_n, rc_n);
        fw_acc ^= fw;
        rc_acc ^= rc;

        // update per-lane rotation amounts
        fw_n = fw_n.wrapping_sub(LANES);
        rc_n = rc_n.wrapping_add(LANES);
    }

    if !remainder.is_empty() {
        let v: Simd<u8, LANES> = Simd::load_or_default(remainder);
        let (fw, rc) = process(v, fw_n, rc_n);
        fw_acc ^= fw;
        rc_acc ^= rc;
    }

    (fw_acc.reduce_xor(), rc_acc.reduce_xor())
}

#[cfg(test)]
mod tests {
    use rand::{RngExt, SeedableRng};
    use rstest::rstest;

    /// Generate a sequence of DNA bases of length `len`. All values are guaranteed to be valid, and the
    /// RNG is seeded with a constant for reproducability.
    fn valid_bases(len: usize) -> Vec<u8> {
        const ALPHABET: &[u8; 4] = b"ACGT";
        let mut rng = rand::rngs::SmallRng::seed_from_u64(0x1badb007deadbeef);
        (0..len).map(|_| ALPHABET[rng.random_range(0..4)]).collect()
    }

    #[rstest]
    fn verify_against_scalar(#[values(1, 4, 6, 8, 10, 12, 15, 16, 25, 32, 35, 64, 100, 128)] k: usize) {
        let data = valid_bases(k);
        let h_scal = crate::sketch::FracMinHash::hash_window(&data);
        let h_simd = super::hash_window(&data);
        assert_eq!(h_scal, h_simd);
    }
}
