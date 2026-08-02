//! Honest AVX-512 sketching throughput, including real k-mer emission.
//!
//! The earlier probe (`sketch_simd_probe.rs`) measured hashing only: it
//! counted selected windows but never produced the `{position, hash, strand}`
//! records the index actually needs. That is optimistic — extraction and
//! buffer writes are exactly the part a "hash-only" number hides. This adds
//! them and compares against the real `FracMinHash::sketch_slice_into`, not a
//! hand-rolled scalar reimplementation, on a chunk size representative of what
//! `index.rs::chunk_windows` actually hands a worker (tested at the `-@64`
//! floor of 2^21 windows and the `-@8` size of ~97 Mbase).
//!
//! Run with `cargo test --release --test avx_emit_probe -- --nocapture`.
//! x86_64 + AVX-512F only; skips itself elsewhere.

#![allow(unsafe_op_in_unsafe_fn)]

#[cfg(target_arch = "x86_64")]
mod avx {
    use shmap::sketch::FracMinHash;
    use shmap::types::{Hash, Kmer, RPos};
    use std::arch::x86_64::*;

    const A: u64 = 0x3c8b_fbb3_95c6_0474;
    const C: u64 = 0x3193_c185_62a0_2b4c;
    const G: u64 = 0x2032_3ed0_8257_2324;
    const TN: u64 = 0x2955_49f5_4be2_4456;

    fn table(fw: bool, rot: u32) -> [u64; 8] {
        let base = if fw { [A, C, G, TN] } else { [TN, G, C, A] };
        let mut t = [0u64; 8];
        for i in 0..4 {
            t[i] = base[i].rotate_left(rot % 64);
        }
        t
    }

    fn scalar_luts() -> ([u64; 256], [u64; 256]) {
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

    /// The winning variant from the earlier probe (eight scalar loads, no
    /// transpose, no gather) plus real emission: on the rare step where a
    /// lane's hash clears the threshold, extract that lane's hash and
    /// position and push a real `Kmer` into that lane's own output buffer.
    /// Concatenating the eight buffers in lane (= position) order reproduces
    /// what `sketch_slice_into` produces for the whole chunk, which the
    /// correctness check below verifies bit for bit.
    #[target_feature(enable = "avx512f")]
    unsafe fn sketch_avx512(s: &[u8], k: usize, offset: RPos, thres: u64) -> [Vec<Kmer>; 8] {
        let windows = s.len() - k + 1;
        let per = windows / 8;
        let (fw_lut, rc_lut) = scalar_luts();

        let mut init_fw = [0u64; 8];
        let mut init_rc = [0u64; 8];
        for j in 0..8 {
            let start = j * per;
            let (mut f, mut r) = (0u64, 0u64);
            for i in 0..k {
                let c = s[start + i] as usize;
                f ^= fw_lut[c].rotate_left((k - i - 1) as u32);
                r ^= rc_lut[c].rotate_left(i as u32);
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
        let thres_v = _mm512_set1_epi64(thres as i64);

        // Sized for h_frac ~0.01: mean + slack, same reasoning
        // `expected_capacity` in sketch.rs already uses.
        let cap = (per as f64 * 0.01 + 6.0 * (per as f64 * 0.01).sqrt()) as usize + 16;
        let mut out: [Vec<Kmer>; 8] = std::array::from_fn(|_| Vec::with_capacity(cap));

        let mut code = [4u8; 256];
        for (i, &b) in b"ACGT".iter().enumerate() {
            code[b as usize] = i as u8;
            code[(b + 32) as usize] = i as u8;
        }
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

        // Window 0 of each lane (the initial window, r = k-1+offset+lane*per).
        for j in 0..8usize {
            let h = init_fw[j] ^ init_rc[j];
            if h <= thres {
                let r = (k - 1 + j * per) as RPos + offset;
                out[j].push(Kmer::new(r, h as Hash, init_fw[j] > init_rc[j]));
            }
        }

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

            let mask = _mm512_cmple_epu64_mask(h, thres_v);
            if mask != 0 {
                // Rare (~1% of steps): extract only the lanes that hit.
                let mut hv = [0u64; 8];
                let mut fv = [0u64; 8];
                let mut rv = [0u64; 8];
                _mm512_storeu_si512(hv.as_mut_ptr() as *mut __m512i, h);
                _mm512_storeu_si512(fv.as_mut_ptr() as *mut __m512i, h_fw);
                _mm512_storeu_si512(rv.as_mut_ptr() as *mut __m512i, h_rc);
                for j in 0..8usize {
                    if (mask >> j) & 1 == 1 {
                        let r = (k - 1 + step + j * per) as RPos + offset;
                        out[j].push(Kmer::new(r, hv[j] as Hash, fv[j] > rv[j]));
                    }
                }
            }
        }
        out
    }

    pub fn run() {
        if !is_x86_feature_detected!("avx512f") {
            println!("no avx512f on this host; skipping");
            return;
        }

        fn pseudo_random_dna(seed: u64, len: usize) -> Vec<u8> {
            let bases = b"ACGT";
            let mut state = seed;
            (0..len)
                .map(|_| {
                    state ^= state << 13;
                    state ^= state >> 7;
                    state ^= state << 17;
                    bases[(state % 4) as usize]
                })
                .collect()
        }

        let k = 25usize;
        let h_frac = 0.01f64;
        let thres = (h_frac * u64::MAX as f64) as u64;
        let sketcher = FracMinHash::new(k as i32, h_frac);

        // Both the -@64 floor chunk (2^21 windows) and the -@8 chunk (~97
        // Mbase) from `chunk_windows`, since realistic chunk size depends on
        // thread count.
        for (label, len) in [
            ("~2^21 windows (-@64 floor)", (1usize << 21) + k),
            ("~97 Mbase (-@8 chunk)", 97_000_000),
        ] {
            let seq = pseudo_random_dna(0x00C0_FFEE ^ len as u64, len);

            // Correctness first: concatenated AVX-512 output must equal the
            // real sketch_slice_into's output bit for bit.
            let scalar = sketcher.sketch_slice_into(&seq, 0, Vec::new());
            let avx = unsafe { sketch_avx512(&seq, k, 0, thres) };
            let concatenated: Vec<Kmer> = avx.into_iter().flatten().collect();
            let ok = scalar.len() == concatenated.len()
                && scalar
                    .iter()
                    .zip(&concatenated)
                    .all(|(a, b)| a.r == b.r && a.h == b.h && a.strand == b.strand);
            println!(
                "\n  {label}: {} windows, correctness = {}",
                len - k + 1,
                if ok { "MATCH" } else { "MISMATCH" }
            );
            if !ok {
                println!("  scalar len={} avx len={}", scalar.len(), concatenated.len());
                for i in 0..scalar.len().min(concatenated.len()).min(5) {
                    println!(
                        "    [{i}] scalar={:?} avx={:?}",
                        (scalar[i].r, scalar[i].h, scalar[i].strand),
                        (concatenated[i].r, concatenated[i].h, concatenated[i].strand)
                    );
                }
                continue;
            }

            // Warm allocator/branch predictor identically.
            let _ = sketcher.sketch_slice_into(&seq[..1_000_000.min(seq.len())], 0, Vec::new());
            let _ = unsafe { sketch_avx512(&seq[..(1usize << 21) + k], k, 0, thres) };

            let t0 = std::time::Instant::now();
            let s_out = sketcher.sketch_slice_into(&seq, 0, Vec::new());
            let scalar_s = t0.elapsed().as_secs_f64();

            let t1 = std::time::Instant::now();
            let a_out = unsafe { sketch_avx512(&seq, k, 0, thres) };
            let avx_s = t1.elapsed().as_secs_f64();

            let mb = len as f64 / 1e6;
            println!(
                "  real sketch_slice_into   {scalar_s:.4} s  ({:.1} Mbase/s)  {} kmers",
                mb / scalar_s,
                s_out.len()
            );
            println!(
                "  AVX-512 + real emission   {avx_s:.4} s  ({:.1} Mbase/s)  {} kmers",
                mb / avx_s,
                a_out.iter().map(|v| v.len()).sum::<usize>()
            );
            println!("  speedup                   {:.2}x", scalar_s / avx_s);
        }
        println!();
    }
}

#[test]
fn honest_avx512_throughput_including_emission() {
    #[cfg(target_arch = "x86_64")]
    avx::run();
    #[cfg(not(target_arch = "x86_64"))]
    println!("not x86_64; skipping");
}
