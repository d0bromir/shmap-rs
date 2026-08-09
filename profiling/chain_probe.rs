//! Q10 addendum: is the rolling-hash loop dependency-chain bound?
//!
//! The 2-bit packing probe measured the baseline roll at ~1.01 ns/base.
//! On this host (~2.9-3.0 GHz sustained, single core) that is ~3 cycles
//! per base — suspiciously exactly the length of the serial chain each
//! hash accumulator carries: `rotate` -> `xor` -> `xor`, each 1 cycle,
//! each depending on the last, with the next base's rotate depending on
//! this base's result.
//!
//! If that is the real bottleneck, then *load* count is not — and both
//! Q1's SIMD attempt and Q10's 2-bit packing were aimed at the wrong
//! thing. The test: run N independent hash chains over N disjoint slices
//! of the sequence, interleaved in one loop. Independent chains fill the
//! pipeline's idle cycles while each chain waits on its own dependency.
//!
//!   - dependency-bound  => 2 chains ~= 2x total throughput, 4 chains more
//!   - load/port-bound   => extra chains contend, little or no gain
//!
//! This is not a proposed optimization — sketching one sequence is
//! inherently one chain. It is a diagnostic for *why* the loop costs what
//! it does, and therefore what (if anything) could ever move it.
//!
//! **Read only the 1-3 chain rows.** At 4+ chains the measurement
//! collapses ~6x, which is a codegen artifact of this probe rather than a
//! property of the hardware: LLVM fully unrolls the inner `for c in
//! 0..CHAINS` loop and keeps every accumulator in a register up to 3
//! chains, but stops doing so beyond that, at which point `h_fw[c]`/
//! `h_rc[c]` spill to stack and every step pays a load+store. Those rows
//! measure the spill. Not chased further — the 1->3 trend already answers
//! the question this probe exists for.
//!
//! Build and run: `rustc -O -C target-cpu=native chain_probe.rs -o
//! /tmp/chain_probe && /tmp/chain_probe`

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

struct Luts {
    fw: [Hash; 256],
    rc: [Hash; 256],
    fw_k: [Hash; 256],
    rc_r1: [Hash; 256],
    rc_k1: [Hash; 256],
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
    Luts { fw, rc, fw_k, rc_r1, rc_k1 }
}

fn random_sequence(n: usize, mut seed: u64) -> Vec<u8> {
    let bases = [b'A', b'C', b'G', b'T'];
    (0..n).map(|_| bases[(splitmix64(&mut seed) & 3) as usize]).collect()
}

#[inline(always)]
fn init_window(s: &[u8], lut: &Luts) -> (Hash, Hash) {
    let mut h_fw: Hash = 0;
    let mut h_rc: Hash = 0;
    for (i, &c) in s[..K].iter().enumerate() {
        let c = c as usize;
        h_fw ^= lut.fw[c].rotate_left((K - i - 1) as u32);
        h_rc ^= lut.rc[c].rotate_left(i as u32);
    }
    (h_fw, h_rc)
}

/// `CHAINS` independent rolling hashes over `CHAINS` disjoint slices,
/// advanced in lockstep inside one loop. Returns (xor of all, bases rolled).
fn roll_chains<const CHAINS: usize>(s: &[u8], lut: &Luts) -> (u64, u64) {
    let per = s.len() / CHAINS;
    debug_assert!(per > K + 8);
    let steps = per - K;

    let mut h_fw = [0u64; CHAINS];
    let mut h_rc = [0u64; CHAINS];
    for c in 0..CHAINS {
        let (f, r) = init_window(&s[c * per..], lut);
        h_fw[c] = f;
        h_rc[c] = r;
    }

    let mut acc = 0u64;
    for i in 0..steps {
        for c in 0..CHAINS {
            let base = c * per;
            let in_c = s[base + K + i] as usize;
            let out_c = s[base + i] as usize;
            h_fw[c] = h_fw[c].rotate_left(1) ^ lut.fw_k[out_c] ^ lut.fw[in_c];
            h_rc[c] = h_rc[c].rotate_right(1) ^ lut.rc_r1[out_c] ^ lut.rc_k1[in_c];
            acc ^= h_fw[c] ^ h_rc[c];
        }
    }
    (acc, (steps * CHAINS) as u64)
}

fn main() {
    let n = 48_000_000usize;
    let s = random_sequence(n, 0xC0FFEE);
    let lut = make_luts();
    println!("sequence: {n} bases, k={K}");
    println!("(ns/base is per base *rolled*, so more chains = more total work)\n");

    for trial in 1..=3 {
        println!("-- trial {trial} --");
        let mut one_chain_ns = 0.0f64;

        macro_rules! run {
            ($n:literal) => {{
                let t0 = Instant::now();
                let (acc, cnt) = roll_chains::<$n>(&s, &lut);
                let dt = t0.elapsed();
                black_box(acc);
                let ns = dt.as_nanos() as f64 / cnt as f64;
                if $n == 1 {
                    one_chain_ns = ns;
                }
                println!(
                    "{:2} chain(s)   {ns:6.3} ns/base   ({:.2}x the throughput of 1 chain)",
                    $n,
                    one_chain_ns / ns
                );
            }};
        }

        run!(1);
        run!(2);
        run!(3);
        run!(4);
        run!(6);
        run!(8);
        println!();
    }
}
