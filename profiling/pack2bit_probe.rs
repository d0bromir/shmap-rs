//! Q10: does 2-bit-packed sequence storage speed up the rolling-hash loop?
//!
//! `RESULTS.md` §11 names 2-bit packing as one of
//! the two levers left for sketching (~2.0 ns/base, 19.3% of `mapping`
//! plus a large share of indexing). The other lever, SIMD, was tried in
//! Q1 and lost. Q1's own conclusion about why is the premise here: the
//! loop does six L1 loads per base — two sequence bytes and four LUT
//! entries — and "the lever is removing loads, not adding independent
//! chains."
//!
//! 2-bit packing attacks exactly that: 4 bases per byte means one
//! sequence load can serve 4 iterations instead of 1, cutting the
//! sequence-load share from 2/base to 0.5/base. The LUT loads are
//! untouched, so the ceiling is a ~25% reduction in total loads — worth
//! measuring, not obviously worth implementing.
//!
//! Faithful to `src/sketch.rs`'s `sketch_slice_into`: same five LUT
//! tables, same rotate/xor pattern, same two scan positions (`in` at r,
//! `out` at r-k) advancing in lockstep. **All three variants produce
//! bit-identical accumulators**, asserted below — that is what makes the
//! packed extraction trustworthy, not just fast.
//!
//! Three variants:
//!   1. baseline  -- byte-per-base, exactly the real loop's structure
//!   2. packed    -- naive: reload the cached byte when a stream crosses a
//!                   byte boundary (a predictable branch, 1-in-4)
//!   3. packed_u4 -- unrolled by 4: one unaligned u64 load per stream per
//!                   group of 4 bases, no branch. This is 2-bit packing's
//!                   best case; testing only variant 2 would strawman it.
//!
//! ACGT only. Real 2-bit packing cannot represent N/ambiguity codes at
//! all, so a real implementation needs a fallback path for them — not
//! modeled here. This probe answers whether the *clean* case is even
//! worth that added complexity before any of it gets designed.
//!
//! Build and run: `rustc -O -C target-cpu=native pack2bit_probe.rs -o
//! /tmp/pack2bit_probe && /tmp/pack2bit_probe`

use std::convert::TryInto;
use std::hint::black_box;
use std::time::Instant;

type Hash = u64;

const A: Hash = 0x3c8b_fbb3_95c6_0474;
const C: Hash = 0x3193_c185_62a0_2b4c;
const G: Hash = 0x2032_3ed0_8257_2324;
const TN: Hash = 0x2955_49f5_4be2_4456;

const K: usize = 25;

fn splitmix64(x: &mut u64) -> u64 {
    *x = x.wrapping_add(0x9E3779B97F4A7C15);
    let mut z = *x;
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
    z ^ (z >> 31)
}

/// The five tables `FracMinHash` holds, plus code-indexed (0..4) copies of
/// the four the rolling step uses.
struct Luts {
    fw: [Hash; 256],
    rc: [Hash; 256],
    fw_k: [Hash; 256],
    rc_r1: [Hash; 256],
    rc_k1: [Hash; 256],
    // code-indexed (0=A, 1=C, 2=G, 3=T)
    c_fw: [Hash; 4],
    c_rc: [Hash; 4],
    c_fw_k: [Hash; 4],
    c_rc_r1: [Hash; 4],
    c_rc_k1: [Hash; 4],
}

fn make_luts() -> Luts {
    let mut fw = [0u64; 256];
    let mut rc = [0u64; 256];
    for &(lo, up, v) in &[(b'a', b'A', A), (b'c', b'C', C), (b'g', b'G', G), (b't', b'T', TN)] {
        fw[lo as usize] = v;
        fw[up as usize] = v;
    }
    for &(lo, up, v) in &[(b'a', b'A', TN), (b'c', b'C', G), (b'g', b'G', C), (b't', b'T', A)] {
        rc[lo as usize] = v;
        rc[up as usize] = v;
    }
    let k = K as u32;
    let mut fw_k = [0u64; 256];
    let mut rc_r1 = [0u64; 256];
    let mut rc_k1 = [0u64; 256];
    for c in 0..256 {
        fw_k[c] = fw[c].rotate_left(k);
        rc_r1[c] = rc[c].rotate_right(1);
        rc_k1[c] = rc[c].rotate_left(k - 1);
    }
    let bases = [b'A', b'C', b'G', b'T'];
    let mut c_fw = [0u64; 4];
    let mut c_rc = [0u64; 4];
    let mut c_fw_k = [0u64; 4];
    let mut c_rc_r1 = [0u64; 4];
    let mut c_rc_k1 = [0u64; 4];
    for (code, &b) in bases.iter().enumerate() {
        c_fw[code] = fw[b as usize];
        c_rc[code] = rc[b as usize];
        c_fw_k[code] = fw_k[b as usize];
        c_rc_r1[code] = rc_r1[b as usize];
        c_rc_k1[code] = rc_k1[b as usize];
    }
    Luts { fw, rc, fw_k, rc_r1, rc_k1, c_fw, c_rc, c_fw_k, c_rc_r1, c_rc_k1 }
}

/// Random ACGT sequence as plain bytes and as its 2-bit packing (4
/// bases/byte, little-endian within the byte). The packed buffer carries 8
/// bytes of tail padding so the unrolled variant's u64 loads never run off
/// the end.
fn random_sequence(n: usize, mut seed: u64) -> (Vec<u8>, Vec<u8>) {
    let bases = [b'A', b'C', b'G', b'T'];
    let mut plain = Vec::with_capacity(n);
    let mut packed = vec![0u8; n.div_ceil(4) + 8];
    for i in 0..n {
        let code = (splitmix64(&mut seed) & 3) as u8;
        plain.push(bases[code as usize]);
        packed[i / 4] |= code << ((i % 4) * 2);
    }
    (plain, packed)
}

/// Baseline: byte-per-base, mirroring `sketch_slice_into`'s zipped-iterator
/// form. Returns (xor of every window hash, window count).
fn sketch_baseline(s: &[u8], lut: &Luts) -> (u64, u64) {
    let mut h_fw: Hash = 0;
    let mut h_rc: Hash = 0;
    for (i, &c) in s[..K].iter().enumerate() {
        let c = c as usize;
        h_fw ^= lut.fw[c].rotate_left((K - i - 1) as u32);
        h_rc ^= lut.rc[c].rotate_left(i as u32);
    }
    let mut acc = h_fw ^ h_rc;
    let mut cnt = 1u64;
    for (&in_c, &out_c) in s[K..].iter().zip(s.iter()) {
        let (in_c, out_c) = (in_c as usize, out_c as usize);
        h_fw = h_fw.rotate_left(1) ^ lut.fw_k[out_c] ^ lut.fw[in_c];
        h_rc = h_rc.rotate_right(1) ^ lut.rc_r1[out_c] ^ lut.rc_k1[in_c];
        acc ^= h_fw ^ h_rc;
        cnt += 1;
    }
    (acc, cnt)
}

#[inline(always)]
fn code_at(packed: &[u8], pos: usize) -> usize {
    ((packed[pos / 4] >> ((pos % 4) * 2)) & 0b11) as usize
}

/// Both packed variants share this init, so all three variants start from
/// an identical `(h_fw, h_rc)` — the precondition for asserting their
/// accumulators match.
#[inline(always)]
fn packed_init(packed: &[u8], lut: &Luts) -> (Hash, Hash) {
    let mut h_fw: Hash = 0;
    let mut h_rc: Hash = 0;
    for i in 0..K {
        let c = code_at(packed, i);
        h_fw ^= lut.c_fw[c].rotate_left((K - i - 1) as u32);
        h_rc ^= lut.c_rc[c].rotate_left(i as u32);
    }
    (h_fw, h_rc)
}

/// Packed, naive: keep each stream's current byte in a register, reload it
/// only when that stream crosses a byte boundary.
fn sketch_packed(packed: &[u8], n: usize, lut: &Luts) -> (u64, u64) {
    let (mut h_fw, mut h_rc) = packed_init(packed, lut);
    let mut acc = h_fw ^ h_rc;
    let mut cnt = 1u64;

    let mut in_pos = K;
    let mut out_pos = 0usize;
    let mut in_byte = packed[in_pos / 4];
    let mut out_byte = packed[out_pos / 4];
    for _ in 0..(n - K) {
        if in_pos % 4 == 0 {
            in_byte = packed[in_pos / 4];
        }
        if out_pos % 4 == 0 {
            out_byte = packed[out_pos / 4];
        }
        let in_c = ((in_byte >> ((in_pos % 4) * 2)) & 0b11) as usize;
        let out_c = ((out_byte >> ((out_pos % 4) * 2)) & 0b11) as usize;
        h_fw = h_fw.rotate_left(1) ^ lut.c_fw_k[out_c] ^ lut.c_fw[in_c];
        h_rc = h_rc.rotate_right(1) ^ lut.c_rc_r1[out_c] ^ lut.c_rc_k1[in_c];
        acc ^= h_fw ^ h_rc;
        cnt += 1;
        in_pos += 1;
        out_pos += 1;
    }
    (acc, cnt)
}

/// One unaligned u64 load at byte `p/4` covers positions `4*(p/4) ..
/// 4*(p/4)+32`, so any 4 consecutive positions from `p` fit inside it
/// regardless of alignment. Returns the four 2-bit codes starting at `p`.
#[inline(always)]
fn four_codes(packed: &[u8], p: usize) -> [usize; 4] {
    let b = p / 4;
    let w = u64::from_le_bytes(packed[b..b + 8].try_into().unwrap());
    let sh = (p % 4) * 2;
    let v = w >> sh;
    [
        (v & 0b11) as usize,
        ((v >> 2) & 0b11) as usize,
        ((v >> 4) & 0b11) as usize,
        ((v >> 6) & 0b11) as usize,
    ]
}

/// Packed, unrolled by 4: one u64 load per stream per group of four bases,
/// no per-base branch. 2-bit packing's best case.
fn sketch_packed_u4(packed: &[u8], n: usize, lut: &Luts) -> (u64, u64) {
    let (mut h_fw, mut h_rc) = packed_init(packed, lut);
    let mut acc = h_fw ^ h_rc;
    let mut cnt = 1u64;

    let total = n - K;
    let groups = total / 4;
    let mut in_pos = K;
    let mut out_pos = 0usize;

    for _ in 0..groups {
        let ins = four_codes(packed, in_pos);
        let outs = four_codes(packed, out_pos);
        for j in 0..4 {
            let (in_c, out_c) = (ins[j], outs[j]);
            h_fw = h_fw.rotate_left(1) ^ lut.c_fw_k[out_c] ^ lut.c_fw[in_c];
            h_rc = h_rc.rotate_right(1) ^ lut.c_rc_r1[out_c] ^ lut.c_rc_k1[in_c];
            acc ^= h_fw ^ h_rc;
        }
        cnt += 4;
        in_pos += 4;
        out_pos += 4;
    }
    for _ in 0..(total % 4) {
        let in_c = code_at(packed, in_pos);
        let out_c = code_at(packed, out_pos);
        h_fw = h_fw.rotate_left(1) ^ lut.c_fw_k[out_c] ^ lut.c_fw[in_c];
        h_rc = h_rc.rotate_right(1) ^ lut.c_rc_r1[out_c] ^ lut.c_rc_k1[in_c];
        acc ^= h_fw ^ h_rc;
        cnt += 1;
        in_pos += 1;
        out_pos += 1;
    }
    (acc, cnt)
}

fn main() {
    // ~50 Mbase: far past any cache, and large enough that per-base timing
    // is stable, without the probe taking minutes.
    let n = 50_000_000usize;
    println!("sequence: {n} bases, k={K}");
    println!("plain: {} MB   packed: {} MB", n / 1_000_000, n / 4 / 1_000_000);

    let (plain, packed) = random_sequence(n, 0xC0FFEE);
    let lut = make_luts();

    // Correctness gate: all three must agree bit-for-bit, or the timings
    // below are measuring three different computations and mean nothing.
    let (acc_b, cnt_b) = sketch_baseline(&plain, &lut);
    let (acc_p, cnt_p) = sketch_packed(&packed, n, &lut);
    let (acc_u, cnt_u) = sketch_packed_u4(&packed, n, &lut);
    assert_eq!((acc_b, cnt_b), (acc_p, cnt_p), "naive packed disagrees with baseline");
    assert_eq!((acc_b, cnt_b), (acc_u, cnt_u), "unrolled packed disagrees with baseline");
    println!("all three agree: acc={acc_b:016x}, {cnt_b} windows\n");

    for trial in 1..=3 {
        println!("-- trial {trial} --");
        let mut base_ns = 0.0;
        for (name, f) in [
            ("baseline (byte/base)", &sketch_baseline as &dyn Fn(&[u8], &Luts) -> (u64, u64)),
        ] {
            let t0 = Instant::now();
            let (acc, cnt) = f(&plain, &lut);
            let dt = t0.elapsed();
            black_box(acc);
            base_ns = dt.as_nanos() as f64 / cnt as f64;
            println!("{name:24} {base_ns:6.3} ns/base");
        }
        for (name, f) in [
            ("packed (naive)", &sketch_packed as &dyn Fn(&[u8], usize, &Luts) -> (u64, u64)),
            ("packed (unrolled x4)", &sketch_packed_u4 as &dyn Fn(&[u8], usize, &Luts) -> (u64, u64)),
        ] {
            let t0 = Instant::now();
            let (acc, cnt) = f(&packed, n, &lut);
            let dt = t0.elapsed();
            black_box(acc);
            let ns = dt.as_nanos() as f64 / cnt as f64;
            println!("{name:24} {ns:6.3} ns/base   ({:.2}x baseline)", base_ns / ns);
        }
        println!();
    }
}
