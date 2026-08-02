//! AVX-512 prototype of the rolling ntHash inner loop, for Q1.
//!
//! The scalar loop does six L1 loads per base: two sequence bytes and four
//! lookup-table entries. The multi-lane probe showed it is not latency-bound,
//! so the lever is removing loads, not adding independent chains.
//!
//! AVX-512 removes the four table loads: with bases reduced to 0..4 codes, the
//! table is eight u64s in a register and the lookup is `vpermq`. This is the
//! instruction the SimdMinimizers write-up identified as missing in AVX2, which
//! is why their vectorised attempt lost to scalar there.
//!
//! Eight lanes over eight contiguous blocks. The outgoing base of a step is the
//! incoming base of the step k earlier, so a ring buffer of the last k gathered
//! code-vectors removes the second gather.
//!
//! Hashing only -- no emission. Upper bound on what a real change could get.

#![allow(unsafe_op_in_unsafe_fn)]
use std::arch::x86_64::*;

const A: u64 = 0x3c8b_fbb3_95c6_0474;
const C: u64 = 0x3193_c185_62a0_2b4c;
const G: u64 = 0x2032_3ed0_8257_2324;
const TN: u64 = 0x2955_49f5_4be2_4456;

fn code_of(c: u8) -> u8 {
    match c {
        b'A' | b'a' => 0,
        b'C' | b'c' => 1,
        b'G' | b'g' => 2,
        b'T' | b't' => 3,
        _ => 4,
    }
}

/// `[A, C, G, T, 0(=N), 0, 0, 0]`, rotated left by `rot`, as a permute table.
fn table(fw: bool, rot: u32) -> [u64; 8] {
    let base = if fw { [A, C, G, TN] } else { [TN, G, C, A] };
    let mut t = [0u64; 8];
    for i in 0..4 {
        t[i] = base[i].rotate_left(rot % 64);
    }
    t
}

fn scalar_luts(_k: u32) -> ([u64; 256], [u64; 256]) {
    let mut fw = [0u64; 256];
    let mut rc = [0u64; 256];
    for &(l, u, v) in &[(b'a', b'A', A), (b'c', b'C', C), (b'g', b'G', G), (b't', b'T', TN)] {
        fw[l as usize] = v;
        fw[u as usize] = v;
    }
    for &(l, u, comp) in &[
        (b'a', b'A', b'T'),
        (b'c', b'C', b'G'),
        (b'g', b'G', b'C'),
        (b't', b'T', b'A'),
    ] {
        rc[l as usize] = fw[comp as usize];
        rc[u as usize] = fw[comp as usize];
    }
    (fw, rc)
}

#[target_feature(enable = "avx512f")]
unsafe fn hash_avx512(codes: &[u8], k: usize, thres: u64) -> (u64, usize) {
    let windows = codes.len() - k + 1;
    let per = windows / 8;

    let (fw_lut, rc_lut) = scalar_luts(k as u32);
    // Per-lane initial windows, computed scalar -- O(k) once per lane.
    let mut init_fw = [0u64; 8];
    let mut init_rc = [0u64; 8];
    for j in 0..8 {
        let start = j * per;
        let (mut f, mut r) = (0u64, 0u64);
        for i in 0..k {
            let c = codes[start + i];
            let asc = b"ACGTN"[c as usize];
            f ^= fw_lut[asc as usize].rotate_left((k - i - 1) as u32);
            r ^= rc_lut[asc as usize].rotate_left(i as u32);
        }
        init_fw[j] = f;
        init_rc[j] = r;
    }

    let mut h_fw = _mm512_loadu_si512(init_fw.as_ptr() as *const __m512i);
    let mut h_rc = _mm512_loadu_si512(init_rc.as_ptr() as *const __m512i);

    let t_fw = _mm512_loadu_si512(table(true, 0).as_ptr() as *const __m512i);
    let t_fw_k = _mm512_loadu_si512(table(true, k as u32).as_ptr() as *const __m512i);
    let t_rc_r1 = _mm512_loadu_si512(table(false, 64 - 1).as_ptr() as *const __m512i);
    let t_rc_k1 = _mm512_loadu_si512(table(false, k as u32 - 1).as_ptr() as *const __m512i);

    // Lane byte offsets, and a ring of the last k gathered code vectors so the
    // outgoing base costs no second gather.
    let offsets = _mm512_set_epi64(
        (7 * per) as i64,
        (6 * per) as i64,
        (5 * per) as i64,
        (4 * per) as i64,
        (3 * per) as i64,
        (2 * per) as i64,
        per as i64,
        0,
    );
    let byte_mask = _mm512_set1_epi64(0xFF);
    let mut ring: Vec<__m512i> = Vec::with_capacity(k);
    for t in 0..k {
        let idx = _mm512_add_epi64(offsets, _mm512_set1_epi64(t as i64));
        let g = _mm512_i64gather_epi64(idx, codes.as_ptr() as *const i64, 1);
        ring.push(_mm512_and_si512(g, byte_mask));
    }

    let mut checksum = _mm512_setzero_si512();
    let mut selected = 0usize;
    let thres_v = _mm512_set1_epi64(thres as i64);

    for step in 1..per {
        let in_at = step + k - 1;
        let idx = _mm512_add_epi64(offsets, _mm512_set1_epi64(in_at as i64));
        let g = _mm512_i64gather_epi64(idx, codes.as_ptr() as *const i64, 1);
        let in_c = _mm512_and_si512(g, byte_mask);
        let out_c = ring[(step - 1) % k];
        ring[in_at % k] = in_c;

        // The four table loads of the scalar loop, as four register permutes.
        let a = _mm512_permutexvar_epi64(out_c, t_fw_k);
        let b = _mm512_permutexvar_epi64(in_c, t_fw);
        let c = _mm512_permutexvar_epi64(out_c, t_rc_r1);
        let d = _mm512_permutexvar_epi64(in_c, t_rc_k1);

        h_fw = _mm512_xor_si512(_mm512_xor_si512(_mm512_rol_epi64::<1>(h_fw), a), b);
        h_rc = _mm512_xor_si512(_mm512_xor_si512(_mm512_ror_epi64::<1>(h_rc), c), d);

        let h = _mm512_xor_si512(h_fw, h_rc);
        checksum = _mm512_add_epi64(checksum, h);
        selected += _mm512_cmple_epu64_mask(h, thres_v).count_ones() as usize;
    }

    let mut out = [0u64; 8];
    _mm512_storeu_si512(out.as_mut_ptr() as *mut __m512i, checksum);
    (out.iter().fold(0u64, |a, b| a.wrapping_add(*b)), selected)
}

/// Same eight lanes, but the codes are transposed to step-major order first
/// (`t[i * 8 + j] = codes[block_j + i]`), so a step reads eight *contiguous*
/// bytes instead of gathering eight scattered ones. The transpose is eight
/// sequential reads and one sequential write per eight bytes, which is cheap;
/// the question is whether it is cheaper than the gathers it removes.
#[target_feature(enable = "avx512f")]
unsafe fn hash_avx512_transposed(t: &[u8], per: usize, k: usize, thres: u64, init_fw: &[u64; 8], init_rc: &[u64; 8]) -> (u64, usize) {
    let mut h_fw = _mm512_loadu_si512(init_fw.as_ptr() as *const __m512i);
    let mut h_rc = _mm512_loadu_si512(init_rc.as_ptr() as *const __m512i);

    let t_fw = _mm512_loadu_si512(table(true, 0).as_ptr() as *const __m512i);
    let t_fw_k = _mm512_loadu_si512(table(true, k as u32).as_ptr() as *const __m512i);
    let t_rc_r1 = _mm512_loadu_si512(table(false, 64 - 1).as_ptr() as *const __m512i);
    let t_rc_k1 = _mm512_loadu_si512(table(false, k as u32 - 1).as_ptr() as *const __m512i);

    let mut checksum = _mm512_setzero_si512();
    let mut selected = 0usize;
    let thres_v = _mm512_set1_epi64(thres as i64);

    for step in 1..per {
        let in_at = step + k - 1;
        // One 64-bit load, widened to eight lanes. No gather.
        let in_c = _mm512_cvtepu8_epi64(_mm_loadl_epi64(t.as_ptr().add(in_at * 8) as *const __m128i));
        let out_c = _mm512_cvtepu8_epi64(_mm_loadl_epi64(t.as_ptr().add((step - 1) * 8) as *const __m128i));

        let a = _mm512_permutexvar_epi64(out_c, t_fw_k);
        let b = _mm512_permutexvar_epi64(in_c, t_fw);
        let c = _mm512_permutexvar_epi64(out_c, t_rc_r1);
        let d = _mm512_permutexvar_epi64(in_c, t_rc_k1);

        h_fw = _mm512_xor_si512(_mm512_xor_si512(_mm512_rol_epi64::<1>(h_fw), a), b);
        h_rc = _mm512_xor_si512(_mm512_xor_si512(_mm512_ror_epi64::<1>(h_rc), c), d);

        let h = _mm512_xor_si512(h_fw, h_rc);
        checksum = _mm512_add_epi64(checksum, h);
        selected += _mm512_cmple_epu64_mask(h, thres_v).count_ones() as usize;
    }

    let mut out = [0u64; 8];
    _mm512_storeu_si512(out.as_mut_ptr() as *mut __m512i, checksum);
    (out.iter().fold(0u64, |a, b| a.wrapping_add(*b)), selected)
}

/// Neither gather nor transpose: build the lane vector from eight ordinary
/// scalar byte loads. A gather is one instruction but many uops and long
/// latency; eight loads plus a `set` may retire faster, and it needs no
/// preparation pass at all -- it reads the ASCII sequence directly, mapping
/// bases to codes with a 256-byte table.
#[target_feature(enable = "avx512f")]
unsafe fn hash_avx512_scalarload(s: &[u8], per: usize, k: usize, thres: u64, init_fw: &[u64; 8], init_rc: &[u64; 8], code: &[u8; 256]) -> (u64, usize) {
    let mut h_fw = _mm512_loadu_si512(init_fw.as_ptr() as *const __m512i);
    let mut h_rc = _mm512_loadu_si512(init_rc.as_ptr() as *const __m512i);
    let t_fw = _mm512_loadu_si512(table(true, 0).as_ptr() as *const __m512i);
    let t_fw_k = _mm512_loadu_si512(table(true, k as u32).as_ptr() as *const __m512i);
    let t_rc_r1 = _mm512_loadu_si512(table(false, 64 - 1).as_ptr() as *const __m512i);
    let t_rc_k1 = _mm512_loadu_si512(table(false, k as u32 - 1).as_ptr() as *const __m512i);

    let mut checksum = _mm512_setzero_si512();
    let mut selected = 0usize;
    let thres_v = _mm512_set1_epi64(thres as i64);
    let p = s.as_ptr();

    let load8 = |at: usize| -> __m512i {
        _mm512_set_epi64(
            *code.get_unchecked(*p.add(7 * per + at) as usize) as i64,
            *code.get_unchecked(*p.add(6 * per + at) as usize) as i64,
            *code.get_unchecked(*p.add(5 * per + at) as usize) as i64,
            *code.get_unchecked(*p.add(4 * per + at) as usize) as i64,
            *code.get_unchecked(*p.add(3 * per + at) as usize) as i64,
            *code.get_unchecked(*p.add(2 * per + at) as usize) as i64,
            *code.get_unchecked(*p.add(per + at) as usize) as i64,
            *code.get_unchecked(*p.add(at) as usize) as i64,
        )
    };

    for step in 1..per {
        let in_c = load8(step + k - 1);
        let out_c = load8(step - 1);

        let a = _mm512_permutexvar_epi64(out_c, t_fw_k);
        let b = _mm512_permutexvar_epi64(in_c, t_fw);
        let c = _mm512_permutexvar_epi64(out_c, t_rc_r1);
        let d = _mm512_permutexvar_epi64(in_c, t_rc_k1);

        h_fw = _mm512_xor_si512(_mm512_xor_si512(_mm512_rol_epi64::<1>(h_fw), a), b);
        h_rc = _mm512_xor_si512(_mm512_xor_si512(_mm512_ror_epi64::<1>(h_rc), c), d);

        let h = _mm512_xor_si512(h_fw, h_rc);
        checksum = _mm512_add_epi64(checksum, h);
        selected += _mm512_cmple_epu64_mask(h, thres_v).count_ones() as usize;
    }
    let mut out = [0u64; 8];
    _mm512_storeu_si512(out.as_mut_ptr() as *mut __m512i, checksum);
    (out.iter().fold(0u64, |a, b| a.wrapping_add(*b)), selected)
}

fn main() {
    let len = 50_000_000usize;
    let bases = b"ACGT";
    let mut state = 0x1234_5678_9abc_def0u64;
    let seq: Vec<u8> = (0..len)
        .map(|_| {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            bases[(state % 4) as usize]
        })
        .collect();
    let k = 25usize;
    let thres = (0.01f64 * u64::MAX as f64) as u64;
    let mb = len as f64 / 1e6;

    if !is_x86_feature_detected!("avx512f") {
        println!("no avx512f on this host");
        return;
    }

    // The ASCII -> code pre-pass, timed separately: it is real work a real
    // implementation would have to do or avoid.
    let t = std::time::Instant::now();
    let codes: Vec<u8> = seq.iter().map(|&c| code_of(c)).collect();
    let prepass = t.elapsed().as_secs_f64();

    unsafe {
        let _ = hash_avx512(&codes[..1_000_000], k, thres);
        let t = std::time::Instant::now();
        let (cs, n) = hash_avx512(&codes, k, thres);
        let s = t.elapsed().as_secs_f64();
        println!("\n  AVX-512, 8 lanes, k={k}, {mb:.0} Mbase");
        println!("  hashing only     {s:.3} s  ({:.1} Mbase/s)   selected={n} cs={cs:x}", mb / s);
        println!("  + code pre-pass  {:.3} s  ({:.1} Mbase/s)", s + prepass, mb / (s + prepass));

        // Transposed variant: pay a transpose to remove the gathers.
        let windows = codes.len() - k + 1;
        let per = windows / 8;
        let (fw_lut, rc_lut) = scalar_luts(k as u32);
        let mut init_fw = [0u64; 8];
        let mut init_rc = [0u64; 8];
        for j in 0..8 {
            let start = j * per;
            let (mut f, mut r) = (0u64, 0u64);
            for i in 0..k {
                let asc = b"ACGTN"[codes[start + i] as usize];
                f ^= fw_lut[asc as usize].rotate_left((k - i - 1) as u32);
                r ^= rc_lut[asc as usize].rotate_left(i as u32);
            }
            init_fw[j] = f;
            init_rc[j] = r;
        }

        let tt = std::time::Instant::now();
        let rows = per + k;
        let mut tr = vec![0u8; rows * 8];
        for j in 0..8 {
            let src = &codes[j * per..];
            for i in 0..rows.min(src.len()) {
                tr[i * 8 + j] = src[i];
            }
        }
        let transpose = tt.elapsed().as_secs_f64();

        let _ = hash_avx512_transposed(&tr, per.min(1000), k, thres, &init_fw, &init_rc);
        let t = std::time::Instant::now();
        let (cs2, n2) = hash_avx512_transposed(&tr, per, k, thres, &init_fw, &init_rc);
        let s2 = t.elapsed().as_secs_f64();
        println!("\n  transposed, no gather");
        println!("  hashing only     {s2:.3} s  ({:.1} Mbase/s)   selected={n2} cs={cs2:x}", mb / s2);
        println!("  + transpose      {:.3} s  ({:.1} Mbase/s)", s2 + transpose, mb / (s2 + transpose));
        println!("  + transpose+prep {:.3} s  ({:.1} Mbase/s)", s2 + transpose + prepass, mb / (s2 + transpose + prepass));
        // No preparation at all: read ASCII directly with eight scalar loads.
        let mut code = [4u8; 256];
        for (i, &b) in b"ACGT".iter().enumerate() {
            code[b as usize] = i as u8;
            code[(b + 32) as usize] = i as u8;
        }
        let _ = hash_avx512_scalarload(&seq, per.min(1000), k, thres, &init_fw, &init_rc, &code);
        let t = std::time::Instant::now();
        let (cs3, n3) = hash_avx512_scalarload(&seq, per, k, thres, &init_fw, &init_rc, &code);
        let s3 = t.elapsed().as_secs_f64();
        println!("\n  eight scalar loads, no prep, reads ASCII directly");
        println!("  total            {s3:.3} s  ({:.1} Mbase/s)   selected={n3} cs={cs3:x}", mb / s3);

        println!("\n  scalar reference          726.5 Mbase/s\n");
    }
}
