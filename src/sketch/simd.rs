use std::simd::Simd;
use std::simd::prelude::*;

use super::{A, C, G, TN};
use crate::types::Hash;

/// Processing lanes
const LANES: usize = 4;

/// Bitwise rotate-left where each lane has a different shift amount.
fn simd_rotate_left<const N: usize>(v: Simd<u64, N>, shifts: &Simd<u64, N>) -> Simd<u64, N> {
    let mask = Simd::splat(63);
    let s = shifts & mask;
    let inv_s = (Simd::splat(64) - s) & mask;
    (v << s) | (v >> inv_s)
}

#[inline]
fn process(
    v: Simd<u8, LANES>,
    fw_n: &Simd<u64, LANES>,
    rc_n: &Simd<u64, LANES>,
) -> (Simd<u64, LANES>, Simd<u64, LANES>) {
    // 1. Load constants
    let v = v | Simd::splat(0x20);
    let is_a: Mask<i64, LANES> = v.simd_eq(Simd::splat(b'a')).cast();
    let is_c: Mask<i64, LANES> = v.simd_eq(Simd::splat(b'c')).cast();
    let is_g: Mask<i64, LANES> = v.simd_eq(Simd::splat(b'g')).cast();
    let is_t: Mask<i64, LANES> = v.simd_eq(Simd::splat(b't')).cast();

    let mut fw: Simd<u64, LANES> = Simd::splat(0);
    fw = is_a.select(Simd::splat(A), fw);
    fw = is_c.select(Simd::splat(C), fw);
    fw = is_g.select(Simd::splat(G), fw);
    fw = is_t.select(Simd::splat(TN), fw);

    let mut rc: Simd<u64, LANES> = Simd::splat(0);
    rc = is_a.select(Simd::splat(TN), rc);
    rc = is_c.select(Simd::splat(G), rc);
    rc = is_g.select(Simd::splat(C), rc);
    rc = is_t.select(Simd::splat(A), rc);

    // 2. Rotate the gathered values
    let fw = simd_rotate_left(fw, fw_n);
    let rc = simd_rotate_left(rc, rc_n);

    (fw, rc)
}

#[inline]
pub fn hash_window(s: &[u8]) -> (Hash, Hash) {
    let ks = s.len() as u64;

    let mut fw_acc: Simd<u64, LANES> = Simd::splat(0);
    let mut rc_acc: Simd<u64, LANES> = Simd::splat(0);

    let mut fw_n: Simd<u64, LANES> = Simd::from_array([ks - 1, ks - 2, ks - 3, ks - 4]);
    let mut rc_n: Simd<u64, LANES> = Simd::from_array([0, 1, 2, 3]);

    let mut chunks = s.chunks_exact(LANES);

    for chunk in &mut chunks {
        let v: Simd<u8, LANES> = Simd::from_slice(chunk);
        let (fw, rc) = process(v, &fw_n, &rc_n);

        // 3. update per-lane rotation amounts
        fw_n = fw_n - Simd::splat(LANES as u64) & Simd::splat(63);
        rc_n = rc_n + Simd::splat(LANES as u64) & Simd::splat(63);

        // 4. xor-reduce into accumulators
        fw_acc ^= fw;
        rc_acc ^= rc;
    }

    let remainder = chunks.remainder();
    if !remainder.is_empty() {
        let v: Simd<u8, LANES> = Simd::load_or_default(remainder);
        let (fw, rc) = process(v, &fw_n, &rc_n);
        fw_acc ^= fw;
        rc_acc ^= rc;
    }

    (fw_acc.reduce_xor(), rc_acc.reduce_xor())
}
